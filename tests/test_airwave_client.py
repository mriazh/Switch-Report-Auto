"""Unit tests for switch_report.airwave_client (FR-1, FR-2, design 2.2).

Playwright is mocked -- no browser, no network. `FakePage` emulates the exact
Playwright surface the client uses (goto / wait_for_timeout / expect_navigation
/ locator with `is_visible` / `wait_for` / fill / click / context.cookies
+add_cookies+set_extra_http_headers). Polling and the CSV download are plain
HTTP, so they are tested against a mocked `requests.Session`.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from unittest.mock import patch, MagicMock

import pytest

from switch_report.airwave_client import (
    FORM_OR_LOGIN_SELECTOR,
    REPORT_DEFINITION_ID,
    REPORT_ROW_TITLE,
    AirWaveClient,
    compute_range,
    find_report_id,
)
from switch_report.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # never read the repo's real config/.env
        AIRWAVE_URL="https://192.168.240.196",
        AIRWAVE_USERNAME="user",
        AIRWAVE_PASSWORD="pass",
        REPORT_BASE_DIR=tmp_path / "Report",
        TEMPLATES_DIR=tmp_path / "templates",
        AIRWAVE_POLL_INTERVAL_SECONDS=0,
        AIRWAVE_POLL_TIMEOUT_MINUTES=5,
        AIRWAVE_MAX_RETRIES=3,
    )


_BASE = "https://192.168.240.196"
_ROOT_URL = f"{_BASE}/"
_DEFINITION_URL = f"{_BASE}/reports_definition?definitions_edit=1&id={REPORT_DEFINITION_ID}"
_LIST_URL = f"{_BASE}/nf/reports_list"
_CSV_EXPORT_PREFIX = f"{_BASE}/csv_export.csv"

_USERNAME_KEY = "locator:#login-username-id"
_PASSWORD_KEY = "locator:#login-password-id"
_LOGIN_KEY = "locator:#login-button"
_START_KEY = 'locator:input[name="start_string_681"]'
_END_KEY = 'locator:input[name="end_string_681"]'
_RUN_KEY = 'locator:input[name="definitions_do_and_run"]'
_READY_KEY = f"locator:{FORM_OR_LOGIN_SELECTOR}"


def _csv_export_url(report_id: str) -> str:
    return (
        f"{_CSV_EXPORT_PREFIX}?csv_export_all_lists=1"
        f"&csv_export_list_args=id%3D{report_id}"
        "&csv_export_uri=%2Freport_detail"
    )


# ---------------------------------------------------------------------------
# Fake Playwright surface
# ---------------------------------------------------------------------------


class FakeLocator:
    """One selector, recording is_visible / wait_for / fill / click."""

    def __init__(self, page: "FakePage", key: str):
        self.page = page
        self.key = key

    @property
    def first(self) -> "FakeLocator":
        return self

    def is_visible(self) -> bool:
        return self.key in self.page.visible_keys()

    def wait_for(self, **kw):
        self.page.waited.append((self.key, kw.get("state")))

    def fill(self, value: str, **kw):
        self.page.filled[self.key] = value
        self.page.fill_kwargs[self.key] = kw

    def click(self, **kw):
        self.page.clicked.append(self.key)
        self.page.click_kwargs[self.key] = kw


class FakeContext:
    def __init__(self, page: "FakePage"):
        self.page = page
        self.added_cookies: list[dict] = []
        self.extra_headers: list[dict] = []
        self.browser_cookies = [
            {"name": "MercuryAuthHandlerCookie_AMPAuth", "value": "browser-cookie"}
        ]

    def add_cookies(self, cookies: list[dict]) -> None:
        self.added_cookies.extend(cookies)

    def set_extra_http_headers(self, headers: dict) -> None:
        self.extra_headers.append(headers)

    def cookies(self) -> list[dict]:
        self.page.cookie_reads += 1
        return self.browser_cookies


class FakePage:
    """Records navigations/fills/clicks and whether the inline login dialog
    is showing -- the only browser-side state `trigger_report` branches on."""

    def __init__(self, base_url: str, login_dialog: bool = False):
        self.base_url = base_url.rstrip("/")
        self.url = self.base_url + "/LOGIN"
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.waited: list[tuple[str, str | None]] = []
        self.goto_calls: list[str] = []
        self.goto_wait_untils: list[str | None] = []
        self.timeouts: list[float] = []
        self.fill_kwargs: dict[str, dict] = {}
        self.click_kwargs: dict[str, dict] = {}
        self.navigations: list[dict] = []
        self.cookie_reads = 0
        self.login_dialog = login_dialog
        self.context = FakeContext(self)

    def visible_keys(self) -> set[str]:
        if not self.login_dialog:
            return set()
        return {_USERNAME_KEY, _PASSWORD_KEY, _LOGIN_KEY}

    # -- Playwright surface --------------------------------------------------
    def goto(self, url: str, **kw):
        self.goto_calls.append(url)
        self.goto_wait_untils.append(kw.get("wait_until"))
        self.url = url

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, f"locator:{selector}")

    def wait_for_timeout(self, ms: float) -> None:
        self.timeouts.append(ms)

    @contextmanager
    def expect_navigation(self, **kw) -> Iterator[dict]:
        """Record what ran inside the navigation guard, and under which mode."""
        guard = {"wait_until": kw.get("wait_until"), "clicks": []}
        self.navigations.append(guard)
        before = len(self.clicked)
        yield guard
        guard["clicks"].extend(self.clicked[before:])


# ---------------------------------------------------------------------------
# Fake HTTP surface
# ---------------------------------------------------------------------------


def _html(*rows: str) -> str:
    """A /nf/reports_list page made of the given `<tr>` fragments."""
    return "<html><body><table>" + "".join(rows) + "</table></body></html>"


def _row(status: str = "", report_id: str | None = None,
         title: str = REPORT_ROW_TITLE, href: str | None = None) -> str:
    """One generated-report row as AirWave renders it.

    A live run carries the status text and no report_detail link; a finished
    run has an empty status cell and the link.
    """
    status_cell = f"<td><b>{status}</b></td>" if status else "<td></td>"
    if href is None and report_id is not None:
        href = f"/report_detail?id={report_id}"
    title_cell = (f'<td><a href="{href}">{title}</a></td>' if href
                  else f"<td>{title}</td>")
    return f"<tr>{status_cell}{title_cell}<td>Custom</td></tr>"


def _mock_session(pages: list[str] | None = None, payload: bytes | None = None) -> MagicMock:
    """A `requests.Session` serving `pages` (HTML, in order) or one `payload`.

    With no arguments the caller sets its own `get` behaviour.
    """
    session = MagicMock()
    if pages is not None:
        responses = []
        for html in pages:
            resp = MagicMock()
            resp.text = html
            resp.raise_for_status.return_value = None
            resp.iter_content.return_value = [html.encode()]
            responses.append(resp)
        session.get.side_effect = responses
    elif payload is not None:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.iter_content.return_value = [payload[:10], payload[10:]]
        session.get.return_value = resp
    session.__enter__.return_value = session
    session.__exit__.return_value = None
    return session


def _always_pending_session() -> MagicMock:
    """A session whose report list never resolves -- the retry/timeout path."""
    session = _mock_session()

    def _get(url, **kw):
        resp = MagicMock()
        resp.text = _html(_row(status="Pending"))
        return resp

    session.get.side_effect = _get
    return session


# ---------------------------------------------------------------------------
# compute_range (FR-1 date math)
# ---------------------------------------------------------------------------


def test_compute_range_daily_is_previous_day_12am_to_1159pm():
    start, end = compute_range("daily", datetime(2026, 10, 9))
    assert start.date() == datetime(2026, 10, 8).date()
    assert end.date() == datetime(2026, 10, 8).date()
    assert start.strftime("%I:%M %p") == "12:00 AM"
    assert end.strftime("%I:%M %p") == "11:59 PM"


def test_compute_range_daily_month_rollover():
    start, _ = compute_range("daily", datetime(2026, 10, 1))
    assert start.date() == datetime(2026, 9, 30).date()


def test_compute_range_monthly_is_previous_month_day1_to_last_day():
    start, end = compute_range("monthly", datetime(2026, 10, 1))
    assert start.date() == datetime(2026, 9, 1).date()
    assert end.date() == datetime(2026, 9, 30).date()
    assert start.strftime("%I:%M %p") == "12:00 AM"
    assert end.strftime("%I:%M %p") == "11:59 PM"


def test_compute_range_monthly_feb_leap_year():
    start, end = compute_range("monthly", datetime(2028, 3, 1))
    assert start.date() == datetime(2028, 2, 1).date()
    assert end.date() == datetime(2028, 2, 29).date()


def test_compute_range_bad_mode_raises():
    with pytest.raises(ValueError):
        compute_range("weekly", datetime(2026, 1, 1))


def test_compute_range_uses_jakarta_zone():
    """Asia/Jakarta boundary: 2026-10-08 23:59 UTC is 06:59 WIB 10/9 -> daily target 10/8."""
    from zoneinfo import ZoneInfo
    now = datetime(2026, 10, 8, 23, 59, tzinfo=ZoneInfo("UTC"))
    start, _ = compute_range("daily", now)
    assert start.date() == datetime(2026, 10, 8).date()


def test_report_definition_id_is_681():
    assert REPORT_DEFINITION_ID == 681


# ---------------------------------------------------------------------------
# login (FR-1)
# ---------------------------------------------------------------------------


def test_login_uses_requests_session_get_then_post_and_saves_cookie(tmp_path):
    """Login uses requests.Session: GET /LOGIN then POST /LOGIN with destination,
    extracts MercuryAuthHandlerCookie_AMPAuth, saves it in storage_state format,
    and adds it to the browser context.

    AirWave returns 403 on GET /LOGIN (establishes Nginx session cookies),
    so raise_for_status is only called on POST /LOGIN.
    """
    page = FakePage(_BASE)
    state_path = tmp_path / "airwave_session.json"
    client = AirWaveClient(_settings(tmp_path))

    mock_session = MagicMock()
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 403  # AirWave returns 403 on unauthenticated GET /LOGIN
    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status.return_value = None
    mock_cookie = MagicMock()
    mock_cookie.name = "MercuryAuthHandlerCookie_AMPAuth"
    mock_cookie.value = "test-auth-cookie-value"
    mock_cookie.path = "/"
    mock_session.cookies = [mock_cookie]
    mock_post_resp.headers = {}

    mock_session.get.return_value = mock_get_resp
    mock_session.post.return_value = mock_post_resp
    mock_session.__enter__.return_value = mock_session
    mock_session.__exit__.return_value = None

    with patch("switch_report.airwave_client.requests.Session", return_value=mock_session) as mock_session_class:
        client.login(page, session_path=state_path)

    mock_session_class.assert_called_once()

    mock_session.get.assert_called_once()
    get_args = mock_session.get.call_args
    assert get_args[0][0] == f"{_BASE}/LOGIN"
    assert get_args[1]["timeout"] == 15

    mock_session.post.assert_called_once()
    post_args = mock_session.post.call_args
    assert post_args[0][0] == f"{_BASE}/LOGIN"
    assert post_args[1]["data"]["destination"] == "/"
    assert post_args[1]["data"]["credential_0"] == "user"
    assert post_args[1]["data"]["credential_1"] == "pass"
    assert post_args[1]["data"]["login"] == "Log In"
    assert post_args[1]["timeout"] == 15

    assert len(page.context.added_cookies) == 1
    cookie = page.context.added_cookies[0]
    assert cookie["name"] == "MercuryAuthHandlerCookie_AMPAuth"
    assert cookie["value"] == "test-auth-cookie-value"
    assert cookie["domain"] == "192.168.240.196"
    assert cookie["path"] == "/"
    assert cookie["httpOnly"] is True
    assert cookie["secure"] is True

    # The SPA's XHR calls need the cookie as an explicit request header too,
    # not just in the jar.
    assert page.context.extra_headers == [{
        "Cookie": "MercuryAuthHandlerCookie_AMPAuth=test-auth-cookie-value"
    }]

    assert state_path.is_file()
    payload = json.loads(state_path.read_text())
    assert "cookies" in payload
    assert payload["cookies"][0]["name"] == "MercuryAuthHandlerCookie_AMPAuth"
    assert payload["cookies"][0]["value"] == "test-auth-cookie-value"


def test_login_reuses_a_valid_stored_cookie_and_headers_the_page(tmp_path):
    """A stored cookie that still answers 200 on /root short-circuits the
    POST /LOGIN (AirWave rate-limits fresh logins) but must still hand the
    cookie to the browser context, header included."""
    page = FakePage(_BASE)
    state_path = tmp_path / "airwave_session.json"
    state_path.write_text(json.dumps({
        "cookies": [{"name": "MercuryAuthHandlerCookie_AMPAuth", "value": "stored-cookie"}],
        "origins": [],
    }))
    client = AirWaveClient(_settings(tmp_path))

    probe = MagicMock()
    probe.status_code = 200
    mock_session = MagicMock()
    mock_session.get.return_value = probe
    mock_session.__enter__.return_value = mock_session
    mock_session.__exit__.return_value = None

    with patch("switch_report.airwave_client.requests.Session", return_value=mock_session):
        client.login(page, session_path=state_path)

    mock_session.post.assert_not_called()
    assert client._auth_cookie == "stored-cookie"
    assert page.context.added_cookies[0]["value"] == "stored-cookie"
    assert page.context.extra_headers == [{
        "Cookie": "MercuryAuthHandlerCookie_AMPAuth=stored-cookie"
    }]


def test_login_falls_back_to_set_cookie_header(tmp_path):
    """When cookie not in session.cookies, fall back to parsing Set-Cookie header."""
    page = FakePage(_BASE)
    state_path = tmp_path / "airwave_session.json"
    client = AirWaveClient(_settings(tmp_path))

    mock_session = MagicMock()
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 403
    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status.return_value = None
    mock_session.cookies = []
    mock_post_resp.headers = {"Set-Cookie": "MercuryAuthHandlerCookie_AMPAuth=header-cookie-value; Path=/; Secure; HttpOnly"}

    mock_session.get.return_value = mock_get_resp
    mock_session.post.return_value = mock_post_resp
    mock_session.__enter__.return_value = mock_session
    mock_session.__exit__.return_value = None

    with patch("switch_report.airwave_client.requests.Session", return_value=mock_session):
        client.login(page, session_path=state_path)

    assert state_path.is_file()
    payload = json.loads(state_path.read_text())
    assert payload["cookies"][0]["name"] == "MercuryAuthHandlerCookie_AMPAuth"
    assert payload["cookies"][0]["value"] == "header-cookie-value"

    assert len(page.context.added_cookies) == 1
    assert page.context.added_cookies[0]["value"] == "header-cookie-value"


# ---------------------------------------------------------------------------
# trigger_report (FR-1)
# ---------------------------------------------------------------------------


def test_trigger_report_opens_definition_url_and_fills_date_range(tmp_path):
    """The SPA is warmed on `/` (2 s), then the definition page is opened at
    `/reports_definition?definitions_edit=1&id=681`. Whichever renders first --
    the form or the login dialog -- is awaited on a combined selector. With a
    live session it is the form and nothing is submitted."""
    page = FakePage(_BASE)
    client = AirWaveClient(_settings(tmp_path))
    start, end = compute_range("daily", datetime(2026, 10, 9))

    client.trigger_report(page, start, end)

    assert page.goto_calls == [_ROOT_URL, _DEFINITION_URL]
    assert page.goto_wait_untils == ["domcontentloaded", "domcontentloaded"]
    assert page.timeouts == [2000]
    assert page.url == _DEFINITION_URL
    assert page.clicked == [_RUN_KEY]
    assert page.waited == [(_READY_KEY, "attached")]
    assert page.filled[_START_KEY] == "10/8/2026 00:00"
    assert page.filled[_END_KEY] == "10/8/2026 23:59"
    # Nothing was submitted: an authenticated page shows no dialog, and the
    # cookie is never read back from the browser.
    assert page.filled == {_START_KEY: "10/8/2026 00:00", _END_KEY: "10/8/2026 23:59"}
    assert page.cookie_reads == 0


def test_trigger_report_clicks_save_and_run_inside_the_navigation_guard(tmp_path):
    """Answering the form navigates to the queued run, so the click must sit
    inside `expect_navigation(wait_until="domcontentloaded")` -- not fire and
    forget while the page is still swapping. The form is filled and run with
    `force=True`: MochaUI leaves these inputs covered by its own overlays."""
    page = FakePage(_BASE)
    client = AirWaveClient(_settings(tmp_path))

    client.trigger_report(page, datetime(2026, 10, 8), datetime(2026, 10, 8))

    assert len(page.navigations) == 1
    assert page.navigations[0]["wait_until"] == "domcontentloaded"
    assert page.navigations[0]["clicks"] == [_RUN_KEY]
    assert page.fill_kwargs == {_START_KEY: {"force": True}, _END_KEY: {"force": True}}
    assert page.click_kwargs[_RUN_KEY] == {"force": True}


def test_trigger_report_logs_in_when_the_inline_dialog_is_showing(tmp_path):
    """An expired session re-prompts inline on the same page: the combined
    selector resolves to the dialog, it is filled and submitted, waited out,
    then the form is filled and run. The refreshed browser cookie is cached so
    the plain-HTTP poll and download behind it stay authenticated."""
    page = FakePage(_BASE, login_dialog=True)
    client = AirWaveClient(_settings(tmp_path))
    start, end = compute_range("daily", datetime(2026, 10, 9))

    client.trigger_report(page, start, end)

    assert page.filled[_USERNAME_KEY] == "user"
    assert page.filled[_PASSWORD_KEY] == "pass"
    assert page.clicked == [_LOGIN_KEY, _RUN_KEY]
    # The dialog is resolved and waited out before the (slow-to-render) form.
    assert page.waited == [(_READY_KEY, "attached"), (_USERNAME_KEY, "hidden")]
    assert page.navigations[0]["clicks"] == [_RUN_KEY]  # only the run click is guarded
    assert page.cookie_reads == 1
    assert client._auth_cookie == "browser-cookie"


def test_trigger_report_does_not_touch_the_native_nf_endpoint(tmp_path):
    """The SPA page is what the inline login dialog and the `*_681` inputs
    live on; `/nf/reports_definition` is never opened."""
    page = FakePage(_BASE)
    client = AirWaveClient(_settings(tmp_path))
    client.trigger_report(page, datetime(2026, 10, 8), datetime(2026, 10, 8))
    assert all("/nf/" not in url for url in page.goto_calls)


# ---------------------------------------------------------------------------
# find_report_id (HTML parse of /nf/reports_list)
# ---------------------------------------------------------------------------


_PARSE_CASES = [
    # live runs: no report_detail link, and a link alone is not enough
    (_html(_row(status="Pending")), None),
    (_html(_row(status="In Progress")), None),
    (_html(_row(status="Pending", report_id="9")), None),
    # ...and a live status wins even when the row carries a link
    (_html(_row(status="In Progress", report_id="9")), None),
    (_html(_row(status="Pending in queue", report_id="9")), None),
    # blank status cell == done
    (_html(_row(report_id="5356")), "5356"),
    (_html(_row(href="/report_detail?id=5356&x=1")), "5356"),
    # status already blank but the link has not rendered yet -> keep polling
    (_html(_row()), None),
    # only the report_detail `id=` param counts
    (_html(_row(href="/report_detail?report_detail_id=7")), None),
    # no matching row at all
    (_html(_row(status="Pending", title="Some Other Report")), None),
    (_html(), None),
    # AirWave lists generated reports newest-first, so the newest matching row
    # is the one that was just triggered -- an older finished run is never
    # mistaken for it, even when the newest is still queued.
    (_html(_row(report_id="8"), _row(title="Some Other Report", report_id="42"),
           _row(report_id="9")), "8"),
    (_html(_row(status="Pending"), _row(report_id="8")), None),
]


@pytest.mark.parametrize("html, expected", _PARSE_CASES, ids=range(len(_PARSE_CASES)))
def test_find_report_id_parses_the_native_reports_list(html, expected):
    assert find_report_id(html) == expected


def test_find_report_id_ignores_markup_noise_and_entities():
    html = _html(
        "<tr><td>10/11/2026 2:48 PM WIB</td>"
        f'<td><a href="/report_detail?id=5372">{REPORT_ROW_TITLE}</a></td>'
        "<td>Custom</td><td>Groups <a href=\"/ap_group_monitoring?id=2\">Core</a>,</td></tr>"
    )
    assert find_report_id(html) == "5372"
    assert find_report_id("&lt;tr&gt;no rows here&lt;/tr&gt;") is None


# ---------------------------------------------------------------------------
# poll_report_status (FR-2)
# ---------------------------------------------------------------------------


def test_poll_returns_report_id_after_pending(tmp_path):
    """Polling is plain HTTP now: `requests.Session` GETs /nf/reports_list
    (which answers 200 with the whole table, instantly) and the newest
    `#UP time Report Switch Daily` row yields the report id the CSV export
    endpoint needs. No browser involved while waiting."""
    client = AirWaveClient(_settings(tmp_path), session_path=tmp_path / "s.json")
    client._auth_cookie = "test-auth-cookie-value"
    session = _mock_session([
        _html(_row(status="Pending")),
        _html(_row(status="In Progress")),
        _html(_row(report_id="1234")),
    ])

    with patch("switch_report.airwave_client.requests.Session", return_value=session):
        report_id = client.poll_report_status(interval_sec=0, timeout_sec=60)

    assert report_id == "1234"
    assert session.get.call_count == 3
    assert {call[0][0] for call in session.get.call_args_list} == {_LIST_URL}
    session.cookies.set.assert_called_once_with(
        "MercuryAuthHandlerCookie_AMPAuth", "test-auth-cookie-value",
        domain="192.168.240.196", path="/",
    )


def test_poll_uses_the_saved_session_cookie_when_login_did_not_run(tmp_path):
    """A fresh client (backfill batches) reuses the persisted storage state."""
    state_path = tmp_path / "airwave_session.json"
    state_path.write_text(json.dumps({
        "cookies": [{"name": "MercuryAuthHandlerCookie_AMPAuth", "value": "saved-cookie"}],
        "origins": [],
    }))
    client = AirWaveClient(_settings(tmp_path), session_path=state_path)
    session = _mock_session([_html(_row(report_id="77"))])

    with patch("switch_report.airwave_client.requests.Session", return_value=session):
        assert client.poll_report_status(interval_sec=0, timeout_sec=60) == "77"

    session.cookies.set.assert_called_once_with(
        "MercuryAuthHandlerCookie_AMPAuth", "saved-cookie",
        domain="192.168.240.196", path="/",
    )


def test_poll_waits_while_status_is_in_progress(tmp_path):
    """'In Progress' is a live status too -- it must not be read as done."""
    client = AirWaveClient(_settings(tmp_path), session_path=tmp_path / "s.json")
    client._auth_cookie = "c"
    session = _mock_session([
        _html(_row(status="In Progress")),
        _html(_row(report_id="778")),
    ])

    with patch("switch_report.airwave_client.requests.Session", return_value=session):
        assert client.poll_report_status(interval_sec=0, timeout_sec=60) == "778"
    assert session.get.call_count == 2


def test_poll_timeout_raises(tmp_path):
    client = AirWaveClient(_settings(tmp_path), session_path=tmp_path / "s.json")
    client._auth_cookie = "c"
    session = _mock_session([_html(_row(status="Pending"))])

    with patch("switch_report.airwave_client.requests.Session", return_value=session):
        with pytest.raises(TimeoutError):
            client.poll_report_status(interval_sec=0, timeout_sec=1e-9)


def test_poll_no_report_row_times_out(tmp_path):
    """Empty list: no row text and no detail link, so it can never resolve."""
    client = AirWaveClient(_settings(tmp_path), session_path=tmp_path / "s.json")
    client._auth_cookie = "c"
    session = _mock_session([_html()])

    with patch("switch_report.airwave_client.requests.Session", return_value=session):
        with pytest.raises(TimeoutError):
            client.poll_report_status(interval_sec=0, timeout_sec=1e-9)


# ---------------------------------------------------------------------------
# download_csv_zip (FR-2)
# ---------------------------------------------------------------------------


def test_download_csv_zip_hits_the_csv_export_endpoint_with_the_login_cookie(tmp_path):
    """No browser involved: the ZIP comes straight from
    /csv_export.csv?...&csv_export_list_args=id%3D999&csv_export_uri=%2Freport_detail,
    authenticated by the cookie `login()` captured."""
    session = _mock_session(payload=b"PK\x03\x04fake-zip")
    out = tmp_path / "day.zip"
    client = AirWaveClient(_settings(tmp_path), session_path=tmp_path / "s.json")
    client._auth_cookie = "test-auth-cookie-value"

    with patch("switch_report.airwave_client.requests.Session", return_value=session):
        result = client.download_csv_zip("999", out)

    assert result == out
    assert out.is_file()
    assert out.read_bytes() == b"PK\x03\x04fake-zip"
    session.get.assert_called_once()
    assert session.get.call_args[0][0] == _csv_export_url("999")
    assert session.get.call_args[1]["stream"] is True
    # A non-200 must raise instead of writing the error page into the ZIP.
    session.get.return_value.raise_for_status.assert_called_once()
    session.cookies.set.assert_called_once_with(
        "MercuryAuthHandlerCookie_AMPAuth", "test-auth-cookie-value",
        domain="192.168.240.196", path="/",
    )


def test_download_csv_zip_falls_back_to_the_saved_session_file(tmp_path):
    """A fresh client that never called `login()` still reuses the persisted
    storage state, and creates the missing `Raw/<DD>/` parent directories."""
    session = _mock_session(payload=b"PK\x03\x04fake-zip")
    state_path = tmp_path / "airwave_session.json"
    state_path.write_text(json.dumps({
        "cookies": [{"name": "MercuryAuthHandlerCookie_AMPAuth",
                     "value": "saved-cookie"}],
        "origins": [],
    }))
    out = tmp_path / "2026-10" / "Raw" / "08.zip"
    client = AirWaveClient(_settings(tmp_path), session_path=state_path)

    with patch("switch_report.airwave_client.requests.Session", return_value=session):
        client.download_csv_zip("42", out)

    assert out.read_bytes() == b"PK\x03\x04fake-zip"
    session.cookies.set.assert_called_once_with(
        "MercuryAuthHandlerCookie_AMPAuth", "saved-cookie",
        domain="192.168.240.196", path="/",
    )


# ---------------------------------------------------------------------------
# fetch_report (full workflow, 3-trigger retry)
# ---------------------------------------------------------------------------


def _workflow_page(login_dialog: bool = False) -> FakePage:
    return FakePage(_BASE, login_dialog=login_dialog)


def test_fetch_report_daily_full_cycle(tmp_path):
    """Browser drives the definition form only; polling and the download are
    two plain-HTTP GETs on the same session factory."""
    page = _workflow_page()
    client = AirWaveClient(_settings(tmp_path), session_path=tmp_path / "s.json")
    client._auth_cookie = "test-auth-cookie-value"
    out = tmp_path / "2026-10" / "Raw" / "08.zip"

    def _get(url, **kw):
        if "csv_export" in url:
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.iter_content.return_value = [b"PK\x03\x04fake-zip"]
            return resp
        resp = MagicMock()
        resp.text = _html(_row(status="Pending")) if _get.first else _html(_row(report_id="555"))
        _get.first = False
        return resp

    _get.first = True
    session = _mock_session()
    session.get.side_effect = _get

    with patch("switch_report.airwave_client.requests.Session", return_value=session):
        result = client.fetch_report(mode="daily", target_date=datetime(2026, 10, 9),
                                     output_zip_path=out, page=page,
                                     interval_sec=0, timeout_sec=60)

    assert result == out
    assert out.read_bytes() == b"PK\x03\x04fake-zip"
    assert page.goto_calls == [_ROOT_URL, _DEFINITION_URL]
    assert page.clicked == [_RUN_KEY]
    requested = [c[0][0] for c in session.get.call_args_list]
    assert requested == [_LIST_URL, _LIST_URL, _csv_export_url("555")]


def test_fetch_report_retries_trigger_up_to_max(tmp_path):
    """Polls always time out -> trigger re-fired up to AIRWAVE_MAX_RETRIES, then raises."""
    page = _workflow_page()
    client = AirWaveClient(_settings(tmp_path), session_path=tmp_path / "s.json")
    client._auth_cookie = "c"
    out = tmp_path / "Raw" / "08.zip"
    session = _always_pending_session()

    with patch("switch_report.airwave_client.requests.Session", return_value=session):
        with pytest.raises(TimeoutError):
            client.fetch_report(mode="daily", target_date=datetime(2026, 10, 9),
                                output_zip_path=out, page=page,
                                interval_sec=0, timeout_sec=1e-9)

    assert page.clicked.count(_RUN_KEY) == 3  # AIRWAVE_MAX_RETRIES=3


def test_fetch_report_unknown_mode_raises(tmp_path):
    client = AirWaveClient(_settings(tmp_path))
    with pytest.raises(ValueError):
        client.fetch_report(mode="hourly", target_date=datetime(2026, 1, 1),
                            output_zip_path=tmp_path / "z.zip",
                            page=FakePage(_BASE))