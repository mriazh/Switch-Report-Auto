"""CLI orchestrator for the switch-report pipeline (FR-5, design 2.6).

Usage:
    switch-report --mode daily   [--date YYYY-MM-DD] [--skip-download]
    switch-report --mode monthly [--date YYYY-MM-DD] [--skip-download]

Date resolution (Asia/Jakarta):
    daily:   report day = --date if given, else yesterday WIB.
             zip at <base>/YYYY-MM/Raw/<DD>.zip, extract to <DD>/, sheet <DD>.
    monthly: report month = month of --date if given, else previous month WIB.
             zip at <base>/YYYY-MM/Raw/Monthly.zip, extract to Monthly/,
             sheet Monthly.

Orchestration: download (unless --skip-download) -> extract -> inject the
8 workbooks -> notify success or failure with duration + file summary.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import notifier
from .archive_extractor import extract_archive
from .airwave_client import AirWaveClient
from .config import Settings
from .excel_writer import run_all as excel_run_all

WIB = ZoneInfo("Asia/Jakarta")


# ---------------------------------------------------------------------------
# Date resolution
# ---------------------------------------------------------------------------


def _resolve_report_month(ref_day: date) -> tuple[int, int]:
    """Report month = the month *before* `ref_day` (the run/backfill day).

    `ref_day` is the reference day for a monthly run: the day the pipeline
    runs (or a --date backfill day). The report month is always the previous
    month relative to it.
    """
    first = ref_day.replace(day=1)
    prev_last = first - timedelta(days=1)
    return prev_last.year, prev_last.month


def _target_day_without_override(today: datetime, mode: str) -> date:
    """Report reference when --date is not supplied.

    daily:   yesterday (WIB) — the report day is yesterday.
    monthly: today (WIB) — the report month is the previous month.
    """
    today_wib = today.astimezone(WIB).date()
    if mode == "daily":
        return today_wib - timedelta(days=1)
    return today_wib


# ---------------------------------------------------------------------------
# Summary & dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RunSummary:
    success: bool
    mode: str
    report_day: str
    duration_sec: float
    zip_path: Optional[str]
    files_written: list = field(default_factory=list)
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "mode": self.mode,
            "report_day": self.report_day,
            "duration_sec": self.duration_sec,
            "zip_path": self.zip_path,
            "files_written": list(self.files_written),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """Drives one full pipeline run.

    `today` is the aware "current" time; the report reference is derived
    from it (or from `date_override`) inside `run()`.
    """

    def __init__(self, settings: Settings, today: Optional[datetime] = None):
        self._settings = settings
        self._today = (today or datetime.now()).astimezone(WIB) \
            if (today or datetime.now()).tzinfo else \
            (today or datetime.now()).replace(tzinfo=WIB)

    # ------------------------------------------------------------------
    # Download step (Playwright)
    # ------------------------------------------------------------------
    def _download_zip(self, zip_path: Path) -> None:
        """Launch headless Chromium, log in, fetch report 681, save the ZIP.

        This is the only step that touches the live NMS. It is isolated so
        unit tests can monkeypatch it out entirely.

        Always performs a clean login because AirWave session cookies expire
        and cause 500/unauthenticated redirects across daily runs.
        """
        from playwright.sync_api import sync_playwright  # lazy import

        session = self._settings.auth_dir / "airwave_session.json"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            ctx = browser.new_context(ignore_https_errors=True)
            page = ctx.new_page()
            client = AirWaveClient(self._settings, session_path=session)
            # Always perform a clean login — session cookies expire and cause
            # 500/unauthenticated redirects across daily runs.
            client.login(page, session_path=session)
            ctx.storage_state(path=str(session))
            report_day = self._last_report_day
            ref = datetime(report_day.year, report_day.month, report_day.day,
                           tzinfo=WIB)
            client.fetch_report(
                mode=self._last_mode, target_date=ref,
                output_zip_path=zip_path, page=page,
            )
            ctx.close()
            browser.close()

    # ------------------------------------------------------------------
    # Public: run the whole pipeline
    # ------------------------------------------------------------------
    def run(self, *, mode: str, date_override: Optional[date] = None,
            skip_download: bool = False) -> dict:
        """Execute the pipeline and return a dict summary (see RunSummary)."""
        t0 = time.monotonic()
        last_err: Optional[str] = None

        if mode not in ("daily", "monthly"):
            raise ValueError(f"mode must be 'daily' or 'monthly', got {mode!r}")

        ref_day = date_override or _target_day_without_override(self._today, mode)
        self._last_mode = mode
        self._last_report_day = ref_day

        if mode == "daily":
            yy, mm = ref_day.year, ref_day.month
        else:
            yy, mm = _resolve_report_month(ref_day)
        base = self._settings.REPORT_BASE_DIR / f"{yy:04d}-{mm:02d}"
        raw_root = base / "Raw"

        if mode == "daily":
            dd = f"{ref_day.day:02d}"
            zip_name = f"{dd}.zip"
            extract_dir = raw_root / dd
            sheet = dd
            report_day_str = ref_day.isoformat()
        else:
            zip_name = "Monthly.zip"
            extract_dir = raw_root / "Monthly"
            sheet = "Monthly"
            # Monthly report day = the 1st of the report month.
            report_day_str = date(yy, mm, 1).isoformat()
        zip_path = raw_root / zip_name
        files_written: list[str] = []

        # 0. Notify START
        notifier.notify_start(
            settings=self._settings,
            mode=mode,
            target_date_str=report_day_str,
        )

        # 1. Download (unless skipped)
        if skip_download:
            if not zip_path.is_file():
                last_err = f"zip not found for skip-download: {zip_path}"
        else:
            try:
                self._download_zip(zip_path)
            except Exception as err:  # noqa: BLE001
                last_err = f"download failed: {err}"
                print(f"[scheduler] {last_err}", file=sys.stderr)
            if last_err is None and not zip_path.is_file():
                last_err = f"zip not created at {zip_path}"

        # 2. Extract
        if last_err is None:
            try:
                extract_archive(zip_path, extract_dir)
            except Exception as err:  # noqa: BLE001
                last_err = f"extraction failed: {err}"
                print(f"[scheduler] {last_err}", file=sys.stderr)

        # 3. Inject all 8 workbooks
        if last_err is None:
            try:
                out = excel_run_all(
                    output_dir=base / "Output",
                    raw_dir=extract_dir,
                    sheet=sheet,
                    mode=mode,
                    templates_dir=self._settings.TEMPLATES_DIR,
                )
                files_written = [str(p) for p in out]
            except Exception as err:  # noqa: BLE001
                last_err = f"excel injection failed: {err}"
                print(f"[scheduler] {last_err}", file=sys.stderr)

        success = last_err is None
        duration = time.monotonic() - t0
        summary = RunSummary(
            success=success, mode=mode, report_day=report_day_str,
            duration_sec=duration, zip_path=str(zip_path),
            files_written=files_written, error=last_err,
        )

        # 4. Notify result
        notifier.notify(
            settings=self._settings,
            success=success, mode=mode, target_date_str=report_day_str,
            duration_sec=duration, files_written=files_written,
            error=last_err,
        )

        return summary.as_dict()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_date(s: str) -> date:
    """Parse `YYYY-MM-DD` and reject out-of-range values cleanly."""
    import argparse as _a
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise _a.ArgumentTypeError(f"invalid --date value: {s!r} (use YYYY-MM-DD)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="switch-report",
        description="Run the AirWave switch-report pipeline (daily or monthly).",
    )
    p.add_argument("--mode", required=True, choices=("daily", "monthly"),
                   help="Pipeline mode.")
    p.add_argument("--date", type=_parse_date, default=None,
                   help="Backfill report day (daily) or reference month (monthly) as YYYY-MM-DD.")
    p.add_argument("--skip-download", action="store_true",
                   help="Skip the AirWave download; use the existing raw ZIP.")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry. Returns 0 on success, 1 on failure or bad arguments."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        # argparse exits with code 2 on unknown flags / bad choices;
        # map to 1 to keep the exit-code surface uniform.
        return 1

    try:
        settings = Settings()
    except Exception as err:  # missing .env / missing required vars
        print(f"[scheduler] configuration error: {err}", file=sys.stderr)
        return 1

    sched = Scheduler(settings)
    summary = sched.run(mode=args.mode, date_override=args.date,
                        skip_download=args.skip_download)
    if not summary["success"]:
        print(f"[scheduler] FAILED: {summary['error']}", file=sys.stderr)
        return 1
    print(f"[scheduler] OK: {summary['report_day']} "
          f"({len(summary['files_written'])} workbooks in "
          f"{summary['duration_sec']:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())