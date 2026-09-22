"""Backfill AirWave daily reports for a list of days.

Usage:
    python scripts/backfill.py --start 2026-10-01 --end 2026-10-08
    python scripts/backfill.py --start 2026-10-01 --end 2026-10-08 --dates 2026-10-10
    python scripts/backfill.py --dates 2026-10-10 --dry-run

Each day is one report-definition-681 run covering that day
(00:00..23:59 WIB), saved to `<YYYY-MM>/Raw/<DD>.zip`, extracted, and injected
into all 8 workbooks as sheet `<DD>`. Days run sequentially with `--sleep`
seconds in between so the NMS is not hammered.

Date convention differs from `switch-report --date` on purpose: here
`<DD>.zip` and sheet `<DD>` always hold the data *of* day `<DD>`.

No credentials live here; Settings comes from config/.env / .env. No
WhatsApp notifications either -- the console log is the report, since one
message per backfilled day would spam.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from switch_report.airwave_client import AirWaveClient, compute_range
from switch_report.archive_extractor import extract_archive
from switch_report.config import Settings
from switch_report.excel_writer import run_all as excel_run_all
from switch_report.scheduler import _parse_date as parse_date  # same YYYY-MM-DD parsing as the CLI

WIB = ZoneInfo("Asia/Jakarta")

DEFAULT_SLEEP_SECONDS = 10.0

log = logging.getLogger("backfill")


def collect_dates(start: date | None, end: date | None,
                  extra: list[date]) -> list[date]:
    """Sorted unique days from an inclusive `--start`..`--end` range plus
    any explicit `--dates`. Raises ValueError on a malformed or empty set."""
    if (start is None) != (end is None):
        raise ValueError("--start and --end must be given together")
    days = set(extra)
    if start is not None and end is not None:
        if end < start:
            raise ValueError(f"--end {end} is before --start {start}")
        days.update(start + timedelta(days=n) for n in range((end - start).days + 1))
    if not days:
        raise ValueError("no dates given: pass --start/--end and/or --dates")
    return sorted(days)


def target_for_data_day(day: date) -> datetime:
    """Return T such that `compute_range("daily", T)` covers `day` itself.

    Daily mode subtracts a day (`compute_range` returns the previous day), so
    the day we want is targeted as day+1. This is the only place that knows
    about that offset -- if `compute_range` ever changes, fix it here.
    """
    return datetime(day.year, day.month, day.day, tzinfo=WIB) + timedelta(days=1)


def backfill_day(client: AirWaveClient, page, settings: Settings,
                 day: date) -> list[Path]:
    """Pull one day, extract it, inject all 8 workbooks. Returns written files."""
    base = settings.REPORT_BASE_DIR / f"{day.year:04d}-{day.month:02d}"
    sheet = f"{day.day:02d}"
    zip_path = base / "Raw" / f"{sheet}.zip"

    client.fetch_report(
        mode="daily", target_date=target_for_data_day(day),
        output_zip_path=zip_path, page=page,
    )
    extract_archive(zip_path, base / "Raw" / sheet)
    return excel_run_all(
        output_dir=base / "Output",
        raw_dir=base / "Raw" / sheet,
        sheet=sheet, mode="daily",
        templates_dir=settings.TEMPLATES_DIR,
    )


def run_days(settings: Settings, days: list[date],
             sleep_sec: float = DEFAULT_SLEEP_SECONDS) -> list[tuple[date, str]]:
    """Run every day sequentially. Returns [(day, error)] for the failures.

    One browser and one login serve the whole batch; a failing day is logged
    and the batch continues.
    """
    from playwright.sync_api import sync_playwright  # lazy: no browser at import

    failures: list[tuple[date, str]] = []
    session = settings.auth_dir / "airwave_session.json"
    with sync_playwright() as pw:
        import sys
        launch_kwargs = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--ignore-certificate-errors"],
        }
        if sys.platform == "win32":
            launch_kwargs["channel"] = "chrome"
        browser = pw.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()
        client = AirWaveClient(settings, session_path=session)
        client.login(page, session_path=session)

        for i, day in enumerate(days, start=1):
            t0 = time.monotonic()
            log.info("[%d/%d] %s: running report 681 ...", i, len(days), day)
            try:
                files = backfill_day(client, page, settings, day)
                log.info("[%d/%d] %s: OK in %.1fs (%d workbooks)",
                         i, len(days), day, time.monotonic() - t0, len(files))
            except Exception as err:  # noqa: BLE001 - one bad day must not kill the batch
                failures.append((day, str(err)))
                log.error("[%d/%d] %s: FAILED after %.1fs: %s",
                          i, len(days), day, time.monotonic() - t0, err)
            if i < len(days) and sleep_sec > 0:
                log.info("sleeping %.0fs before the next day", sleep_sec)
                time.sleep(sleep_sec)

        ctx.close()
        browser.close()
    return failures


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backfill",
        description="Backfill AirWave daily reports day by day.",
    )
    p.add_argument("--start", type=parse_date, default=None,
                   help="First report day, inclusive (YYYY-MM-DD). Needs --end.")
    p.add_argument("--dates", type=parse_date, nargs="*", default=[],
                   help="Extra individual days, e.g. --dates 2026-10-10.")
    p.add_argument("--end", type=parse_date, default=None,
                   help="Last report day, inclusive (YYYY-MM-DD). Needs --start.")
    p.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_SECONDS,
                   help="Seconds to wait between days (default: 10).")
    p.add_argument("--dry-run", action="store_true",
                   help="Log the plan and exit without touching AirWave.")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = build_parser().parse_args(argv)

    try:
        days = collect_dates(args.start, args.end, args.dates)
    except ValueError as err:
        log.error("%s", err)
        return 1

    log.info("Backfilling %d day(s): %s",
             len(days), ", ".join(d.isoformat() for d in days))
    if args.dry_run:
        for day in days:
            log.info("would backfill %s (%s)", day,
                     compute_range("daily", target_for_data_day(day))[0].date())
        return 0

    try:
        settings = Settings()
    except Exception as err:  # noqa: BLE001 - missing .env / missing vars
        log.error("configuration error: %s", err)
        return 1

    failures = run_days(settings, days, sleep_sec=args.sleep)
    for day, err in failures:
        log.error("failed: %s: %s", day, err)
    if failures:
        log.error("%d/%d day(s) failed", len(failures), len(days))
        return 1
    log.info("all %d day(s) backfilled", len(days))
    return 0


if __name__ == "__main__":
    sys.exit(main())