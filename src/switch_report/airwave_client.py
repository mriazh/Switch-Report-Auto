"""Headless AirWave NMS client (FR-1, FR-2, design.md 2.2).

Logs into the Aruba AirWave web UI with Playwright, triggers report
definition 681 for a computed date range, polls /nf/reports_list until the
run completes, then fetches the CSV export ZIP over plain HTTP. No browser
or network is required at import time; the Playwright `Page` object is
injected so unit tests can drive it with mocks.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from zoneinfo import ZoneInfo

from .config import Settings

if TYPE_CHECKING:
    # Playwright types are duck-typed here so the module imports without
    # a browser install; nothing in the public surface names the concrete
    # Playwright classes.
    from playwright.sync_api import Page

REPORT_DEFINITION_ID = 681
"""Report definition `#UP time Report Switch Daily` on the target AirWave NMS."""

REPORT_ROW_TITLE = "#UP time Report Switch Daily"
"""Row title that appears on /reports_list once definition 681 is triggered."""

CSV_EXPORT_URI = (
    "/csv_export.csv?csv_export_all_lists=1"
    "&csv_export_list_args=id%3D{id}"
    "&csv_export_uri=%2Freport_detail"
)
"""Native CSV-export endpoint for a finished report.

`id%3D{id}` is `id=<report id>` percent-encoded as AirWave expects. This is
the same archive the detail page's "CSV export" link serves, but it is a
plain HTTP GET, so no browser download plumbing is needed.
"""

REPORTS_LIST_URI = "/nf/reports_list"
"""Native generated-reports list.

`/nf/` bypasses the MochaUI SPA wrapper and answers a plain HTTP GET with the
whole table, so polling costs one request instead of a browser navigation.
"""

PAGE_TIMEOUT_MS = 30_000
"""How long the definition page may take to render the dialog or its inputs."""

FORM_OR_LOGIN_SELECTOR = 'input[name="start_string_681"], #login-username-id'
"""First thing the definition page renders: the form, or the login dialog."""

DEFAULT_SESSION_FILE = Path(".auth") / "airwave_session.json"

_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.DOTALL | re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_DETAIL_ID_RE = re.compile(r"[?&]id=(\d+)")
_LIVE_STATUS = ("pending", "in progress")
"""Status cell texts that mean the run has not finished yet."""


def find_report_id(html: str, title: str = REPORT_ROW_TITLE) -> str | None:
    """Report id of the newest `title` row in a /nf/reports_list page, else None.

    AirWave lists generated reports newest-first, so the first row carrying
    the title *is* the run just triggered. While that run is live its status
    cell reads "Pending"/"In Progress" and its `report_detail` link is absent,
    which both resolve to None -- the caller keeps polling instead of
    downloading an older finished run.

    ponytail: regexes over `<tr>`/href rather than a DOM parser; the table
    shape is fixed and `beautifulsoup4` is not a dependency. Row order is the
    only freshness signal, so an NMS build that ever sorts oldest-first needs
    this to look at the last match instead.
    """
    title = title.lower()
    for row in _ROW_RE.findall(html):
        text = unescape(_TAG_RE.sub(" ", row)).lower()
        if title not in text:
            continue
        if any(status in text for status in _LIVE_STATUS):
            return None
        href = next((h for h in _HREF_RE.findall(row) if "report_detail" in h), None)
        if href is None:
            return None
        match = _DETAIL_ID_RE.search(href)
        return match.group(1) if match else None
    return None


def compute_range(mode: str, target_date: datetime) -> tuple[datetime, datetime]:
    """Return (start, end) as Asia/Jakarta-aware datetimes for report 681.

    daily:   previous day 00:00 .. 23:59 WIB.
    monthly: previous month day 1 00:00 .. last day 23:59 WIB.

    `target_date` is interpreted in Asia/Jakarta when naive (matching the
    scheduler's semantics), or converted from its own tz when aware. The
    caller formats the values for the AirWave date picker (`M/D/YYYY HH:MM`)
    at trigger time.
    """
    if mode not in ("daily", "monthly"):
        raise ValueError(f"mode must be 'daily' or 'monthly', got {mode!r}")
    # ponytail: zoneinfo instead of pytz — same result, stdlib on 3.9+.
    tz = ZoneInfo("Asia/Jakarta")
    if target_date.tzinfo is None:
        target = target_date.replace(tzinfo=tz)
    else:
        target = target_date.astimezone(tz)

    if mode == "daily":
        day = (target - timedelta(days=1)).date()
        return (
            datetime(day.year, day.month, day.day, 0, 0, tzinfo=tz),
            datetime(day.year, day.month, day.day, 23, 59, tzinfo=tz),
        )

    first_of_current = target.replace(day=1)
    last_day_prev_month = first_of_current - timedelta(days=1)
    start = first_of_current - timedelta(days=1)  # prev month day 1
    return (
        datetime(start.year, start.month, 1, 0, 0, tzinfo=tz),
        datetime(last_day_prev_month.year, last_day_prev_month.month,
                 last_day_prev_month.day, 23, 59, tzinfo=tz),
    )


class AirWaveClient:
    """Drives one AirWave report run: login -> trigger -> poll -> download."""

    def __init__(self, settings: Settings, session_path: Path | None = None):
        self._s = settings
        self._session_path = Path(session_path) if session_path else DEFAULT_SESSION_FILE
        self._auth_cookie: str | None = None

    # ------------------------------------------------------------------
    # FR-1: authentication
    # ------------------------------------------------------------------
    def login(self, page: "Page", session_path: Path | None = None) -> None:
        """Submit credentials via requests.Session to /LOGIN and persist the storage state.

        Uses requests.Session to GET /LOGIN first (to establish session/CSRF),
        then POST /LOGIN with destination: /, credential_0, credential_1,
        login: Log In. Extracts MercuryAuthHandlerCookie_AMPAuth, saves it to
        `session_path` in Playwright storage_state format, and hands it to
        `page.context` (cookie jar + `Cookie` request header) when `page` is
        provided.
        """
        sp = Path(session_path) if session_path else self._session_path
        sp.parent.mkdir(parents=True, exist_ok=True)

        # Reuse existing valid session cookie to avoid AirWave 403 rate-limits
        cookie = self._stored_auth_cookie(sp)
        if cookie:
            with requests.Session() as test_sess:
                test_sess.verify = False
                if hasattr(test_sess.cookies, "set"):
                    test_sess.cookies.set("MercuryAuthHandlerCookie_AMPAuth", cookie, domain=self._cookie_domain(), path="/")
                try:
                    r = test_sess.get(f"{self._s.AIRWAVE_URL.rstrip('/')}/root", timeout=5)
                    if r.status_code == 200:
                        self._auth_cookie = cookie
                        if page is not None:
                            self._apply_cookie(page, {
                                "name": "MercuryAuthHandlerCookie_AMPAuth",
                                "value": cookie,
                                "domain": self._cookie_domain(),
                                "path": "/",
                            })
                        return
                except Exception:
                    pass

        login_url = self._s.AIRWAVE_URL.rstrip("/") + "/LOGIN"
        payload = {
            "destination": "/",
            "credential_0": self._s.AIRWAVE_USERNAME,
            "credential_1": self._s.AIRWAVE_PASSWORD,
            "login": "Log In",
        }

        with requests.Session() as sess:
            sess.verify = False
            # GET /LOGIN first to establish session and any CSRF tokens
            # AirWave deliberately returns 403 on unauthenticated GET /LOGIN
            # while establishing Nginx session cookies; do not raise on it.
            get_resp = sess.get(login_url, timeout=15)

            # POST /LOGIN with credentials
            resp = sess.post(login_url, data=payload, timeout=15)
            resp.raise_for_status()

            # Extract the MercuryAuthHandlerCookie_AMPAuth cookie from session
            auth_cookie = None
            for cookie in sess.cookies:
                if cookie.name == "MercuryAuthHandlerCookie_AMPAuth":
                    auth_cookie = cookie
                    break

            if auth_cookie is None:
                # Fallback: check Set-Cookie header from POST response
                set_cookie = resp.headers.get("Set-Cookie", "")
                if "MercuryAuthHandlerCookie_AMPAuth" in set_cookie:
                    match = re.search(
                        r"MercuryAuthHandlerCookie_AMPAuth=([^;]+)", set_cookie
                    )
                    if match:
                        cookie_value = match.group(1)
                        auth_cookie = type("Cookie", (), {"name": "MercuryAuthHandlerCookie_AMPAuth", "value": cookie_value, "domain": self._cookie_domain(), "path": "/"})()

        if auth_cookie is None:
            raise RuntimeError("Failed to acquire MercuryAuthHandlerCookie_AMPAuth cookie from AirWave login")

        # Build Playwright storage_state format
        domain = self._cookie_domain()
        storage_state = {
            "cookies": [
                {
                    "name": auth_cookie.name,
                    "value": auth_cookie.value,
                    "domain": domain,
                    "path": getattr(auth_cookie, "path", "/") or "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [],
        }

        # Save storage state to file
        sp.write_text(json.dumps(storage_state))

        # Keep the cookie on the instance so `download_csv_zip` can reuse the
        # same authenticated session for the plain-HTTP CSV export.
        self._auth_cookie = auth_cookie.value

        # If page is provided, add cookie to browser context
        if page is not None:
            self._apply_cookie(page, storage_state["cookies"][0])

    def _apply_cookie(self, page: "Page", cookie: dict) -> None:
        """Put the AirWave auth cookie on `page`'s context both ways.

        The cookie jar entry (`add_cookies`) covers document loads; the
        explicit `Cookie` request header additionally covers the SPA's XHR
        calls, which this NMS build rejects without it.

        ponytail: setting both is belt-and-braces for one NMS quirk -- drop
        `set_extra_http_headers` if a build accepts XHR on the cookie alone.
        """
        page.context.add_cookies([cookie])
        page.context.set_extra_http_headers({
            "Cookie": f"{cookie['name']}={cookie['value']}"
        })

    # ------------------------------------------------------------------
    # FR-1: trigger report 681
    # ------------------------------------------------------------------
    def trigger_report(self, page: "Page", start_dt: datetime, end_dt: datetime) -> None:
        """Open definition 681 on `page`, authenticate if asked, and run it.

        The SPA is warmed with a load of `/` first: on a cold context the
        definition page answers before the MochaUI bundle has taken over the
        document and never recovers on its own. After the warm-up the
        definition page either renders the form inputs or, when the session
        cookie has expired, an inline login dialog (`#login-username-id`) that
        is filled and submitted first.

        This NMS build takes one combined `M/D/YYYY HH:MM` string per bound in
        `start_string_681` / `end_string_681`, and answering the form navigates
        to the queued run, so the click waits for that navigation.

        ponytail: an expired cookie the dialog cannot satisfy surfaces as the
        `wait_for` timeout below. Callers re-run `login` and retry on it.
        """
        base = self._s.AIRWAVE_URL.rstrip("/")
        definition_url = (
            f"{base}/reports_definition"
            f"?definitions_edit=1&id={REPORT_DEFINITION_ID}"
        )
        page.goto(f"{base}/", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        page.goto(definition_url, wait_until="domcontentloaded")

        # Either the form inputs or the login dialog: whichever renders first
        # proves the SPA booted on this page.
        page.locator(FORM_OR_LOGIN_SELECTOR).first.wait_for(
            state="attached", timeout=PAGE_TIMEOUT_MS
        )
        self._dismiss_login_dialog(page)

        page.locator('input[name="start_string_681"]').fill(
            f"{start_dt.month}/{start_dt.day}/{start_dt.year} 00:00", force=True
        )
        page.locator('input[name="end_string_681"]').fill(
            f"{end_dt.month}/{end_dt.day}/{end_dt.year} 23:59", force=True
        )

        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator('input[name="definitions_do_and_run"]').click(force=True)

    def _dismiss_login_dialog(self, page: "Page") -> None:
        """Sign in through AirWave's inline login dialog when it is showing.

        No-op on an authenticated page. When it does log in, the browser's
        refreshed auth cookie is cached so the plain-HTTP poll and CSV download
        that follow stay authenticated without a second round-trip.
        """
        username = page.locator("#login-username-id")
        if not username.is_visible():
            return
        username.fill(self._s.AIRWAVE_USERNAME)
        page.locator("#login-password-id").fill(self._s.AIRWAVE_PASSWORD)
        page.locator("#login-button").click()
        username.wait_for(state="hidden", timeout=PAGE_TIMEOUT_MS)
        for cookie in page.context.cookies():
            if cookie.get("name") == "MercuryAuthHandlerCookie_AMPAuth":
                self._auth_cookie = cookie.get("value")
                break

    # ------------------------------------------------------------------
    # FR-2: poll /nf/reports_list
    # ------------------------------------------------------------------
    def poll_report_status(
        self, timeout_sec: float | None = None, interval_sec: float | None = None,
    ) -> str:
        """Poll /nf/reports_list until the triggered report row has finished.

        Returns the report's detail id (the `id=` query parameter that
        `/report_detail` and `/csv_export.csv` both take). Raises TimeoutError
        when the run does not complete within `timeout_sec`.

        ponytail: a non-200 or unauthenticated answer simply yields no row, so
        the poll keeps trying until the timeout instead of aborting the run on
        a transient error.
        """
        timeout_sec = (self._s.AIRWAVE_POLL_TIMEOUT_MINUTES * 60) if timeout_sec is None \
            else timeout_sec
        interval_sec = (self._s.AIRWAVE_POLL_INTERVAL_SECONDS) if interval_sec is None \
            else interval_sec
        list_url = self._s.AIRWAVE_URL.rstrip("/") + REPORTS_LIST_URI
        deadline = time.monotonic() + timeout_sec
        with requests.Session() as sess:
            self._authenticate_session(sess)
            while True:
                report_id = find_report_id(sess.get(list_url, timeout=15).text)
                if report_id:
                    return report_id
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"AirWave report '{REPORT_ROW_TITLE}' did not complete within "
                        f"{timeout_sec:.0f}s ({interval_sec}s interval)"
                    )
                time.sleep(interval_sec)

    # ------------------------------------------------------------------
    # FR-2: CSV export download
    # ------------------------------------------------------------------
    def download_csv_zip(self, report_id: str, output_zip_path: Path) -> Path:
        """Download report `report_id`'s CSV export ZIP straight over HTTP.

        Uses the authenticated cookie persisted by `login` to GET
        `/csv_export.csv?...&csv_export_list_args=id%3D<report_id>&...`, which
        serves the same archive as the detail page's "CSV export" link without
        a browser download round-trip.
        """
        output_zip_path = Path(output_zip_path)
        url = self._s.AIRWAVE_URL.rstrip("/") + CSV_EXPORT_URI.format(id=report_id)
        with requests.Session() as sess:
            self._authenticate_session(sess)
            resp = sess.get(url, timeout=180, stream=True)
            resp.raise_for_status()
            output_zip_path.parent.mkdir(parents=True, exist_ok=True)
            with output_zip_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if chunk:
                        fh.write(chunk)
        return output_zip_path

    def _authenticate_session(self, sess: "requests.Session") -> None:
        """Put the auth cookie on a plain-HTTP session (self-signed NMS cert)."""
        sess.verify = False
        cookie = self._stored_auth_cookie()
        if cookie:
            sess.cookies.set(
                "MercuryAuthHandlerCookie_AMPAuth", cookie,
                domain=self._cookie_domain(), path="/",
            )

    def _cookie_domain(self) -> str:
        return (self._s.AIRWAVE_URL
                .replace("https://", "").replace("http://", "").split("/")[0])

    def _stored_auth_cookie(self, path: Path | None = None) -> str | None:
        target = Path(path) if path else self._session_path
        """The auth cookie: from this run's `login`, else the saved storage state."""
        if self._auth_cookie:
            return self._auth_cookie
        try:
            state = json.loads(target.read_text())
        except (OSError, ValueError):
            return None
        for cookie in state.get("cookies", []):
            if cookie.get("name") == "MercuryAuthHandlerCookie_AMPAuth":
                return cookie.get("value")
        return None

    # ------------------------------------------------------------------
    # Full workflow with trigger retries (max AIRWAVE_MAX_RETRIES)
    # ------------------------------------------------------------------
    def fetch_report(
        self, mode: str, target_date: datetime, output_zip_path: Path,
        page: "Page" | None = None, *,
        interval_sec: float | None = None, timeout_sec: float | None = None,
    ) -> Path:
        """Run trigger -> poll -> download on an authenticated `page`, retrying
        the trigger up to `AIRWAVE_MAX_RETRIES` times when the poll times out.

        `page` must already be authenticated (see `login`); when omitted this
        raises, so callers always pass the page explicitly. Returns the saved
        ZIP path and raises the last TimeoutError when every attempt fails.
        """
        start, end = compute_range(mode, target_date)
        if page is None:
            raise RuntimeError("fetch_report requires an authenticated page")
        last_err: Exception | None = None
        for attempt in range(1, self._s.AIRWAVE_MAX_RETRIES + 1):
            try:
                self.trigger_report(page, start, end)
                report_id = self.poll_report_status(
                    timeout_sec=timeout_sec, interval_sec=interval_sec,
                )
                return self.download_csv_zip(report_id, output_zip_path)
            except TimeoutError as err:
                last_err = err
                if attempt < self._s.AIRWAVE_MAX_RETRIES:
                    time.sleep(self._s.AIRWAVE_POLL_INTERVAL_SECONDS)
        raise last_err  # type: ignore[misc]