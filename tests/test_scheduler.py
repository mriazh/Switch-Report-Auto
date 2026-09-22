"""Unit tests for switch_report.scheduler (FR-5, NFR-4, design 2.6).

The Playwright download step is monkeypatched on `Scheduler._download_zip`;
the rest (date resolution, skip-download, extraction, excel injection,
notification, error capture) runs for real against tmp dirs with the 9
CSVs zipped up the way the archive extractor expects.
"""
from __future__ import annotations

import zipfile
from datetime import date, datetime
from pathlib import Path

import pytest
from zoneinfo import ZoneInfo

from switch_report import scheduler
from switch_report.config import Settings
from switch_report.archive_extractor import REQUIRED_CSV_PREFIXES

WIB = ZoneInfo("Asia/Jakarta")

# Header names the excel writer expects (mirror of tests/test_excel_writer.py).
AVERAGE = ["Device Uptime", "SNMP Comm Uptime", "ICMP Comm Uptime", "HTTPS Comm Uptime"]


def _mk_zip(zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        ts = "1"
        data = {
            "avg_uptime_by_any_controller": ["98%", "99%", "100%", "-"],
            "avg_uptime_by_any_switch": ["100%", "0%", "55%", "-"],
            "avg_uptime_by_device": ["99%", "99.5%", "98%", "-"],
            "most_utilized_switches": [[f"D{i}", "1 TiB"] for i in range(3)],
            "most_utilized_by_usage": [[f"D{i}", "10", "5", "2 GiB", "43%",
                                         "L", "C", "F", "G"] for i in range(3)],
            "top_cpu_utilization_by_device": [[f"D{i}", "50%", "20%"] for i in range(3)],
            "top_memory_usage_by_device": [[f"D{i}", "70%", "65%"] for i in range(3)],
            "uptime_by_device": [[f"SW{i}", "G", "F", "99.9%", "Yes", "x", "y",
                                  "99%", "0", "98%", "0", "-", "-"] for i in range(2)],
            "device_downtime_detail": [[f"SW{i}", "G", "F", "-", "-", "0",
                                        "0.00%", "0", "0.00%"] for i in range(2)],
        }
        headers = {
            "avg_uptime_by_any_controller": AVERAGE,
            "avg_uptime_by_any_switch": AVERAGE,
            "avg_uptime_by_device": AVERAGE,
            "most_utilized_switches": ["Device", "Total Traffic"],
            "most_utilized_by_usage": ["Device", "Max Clients", "Unique Clients",
                                       "Total Data", "Avg Usage", "Location",
                                       "Controller", "Folder", "Group"],
            "top_cpu_utilization_by_device": ["Device", "Utilization", "CPU Utilization %"],
            "top_memory_usage_by_device": ["Device", "Utilization", "Memory Utilization %"],
            "uptime_by_device": ["Device", "Group", "Folder", "Device Uptime",
                                 "Device Uptime is Reliable", "Time Since Last Contact",
                                 "Time Since Last Boot", "SNMP Comm Uptime",
                                 "SNMP Comm Up/Down Times", "ICMP Comm Uptime",
                                 "ICMP Comm Up/Down Times"],
            "device_downtime_detail": ["Device", "Group", "Folder", "Bootstrap Count",
                                       "Reboot Count", "Device Down Duration",
                                       "Device Downtime Duration (%)",
                                       "Comm Down Duration", "Comm Down Duration (%)"],
        }
        for key, rows in data.items():
            lines = [",".join(headers[key])]
            lines += [",".join(str(c) for c in r) for r in rows]
            zf.writestr(f"#UP_time_Report_Switch_Daily/{key}_{ts}.csv", "\n".join(lines))


def _settings(tmp_path: Path, templates: Path, gowa: bool = False) -> Settings:
    s = Settings(
        _env_file=None,  # never read the repo's real config/.env
        AIRWAVE_URL="https://192.168.240.196",
        AIRWAVE_USERNAME="u",
        AIRWAVE_PASSWORD="p",
        REPORT_BASE_DIR=tmp_path / "Report" / "Switch",
        TEMPLATES_DIR=templates,
        AIRWAVE_POLL_INTERVAL_SECONDS=0,
    )
    if gowa:
        s.GOWA_BASE_URL = "http://gowa.local:8080"
        s.GOWA_TARGET_JID = "628123456789@s.whatsapp.net"
    return s


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    """Seed the 8 template workbooks with Master/Monthly sheets."""
    from openpyxl import Workbook
    from switch_report.excel_writer import WORKBOOK_NAMES
    tdir = tmp_path / "templates"
    tdir.mkdir()
    for name in WORKBOOK_NAMES:
        wb = Workbook()
        wb.active.title = "Master"
        wb.create_sheet("Monthly")
        wb.save(tdir / name)
    return tdir


def test_daily_run_with_date_backfill(tmp_path, templates, monkeypatch):
    src_zip = tmp_path / "source" / "08.zip"
    _mk_zip(src_zip)

    settings = _settings(tmp_path, templates)

    def fake_download(self, zip_path: Path):
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copyfile(src_zip, zip_path)

    monkeypatch.setattr(scheduler.Scheduler, "_download_zip", fake_download)

    s = scheduler.Scheduler(settings, today=datetime(2026, 10, 9, tzinfo=WIB))
    summary = s.run(mode="daily", date_override=date(2026, 10, 8),
                    skip_download=False)

    assert summary["success"] is True
    assert summary["mode"] == "daily"
    assert summary["report_day"] == "2026-10-08"
    assert summary["error"] is None

    zip_used = settings.REPORT_BASE_DIR / "2026-10" / "Raw" / "08.zip"
    assert zip_used.is_file()

    assert len(summary["files_written"]) == 8
    for p in summary["files_written"]:
        assert Path(p).is_file()

    raw_dir = settings.REPORT_BASE_DIR / "2026-10" / "Raw" / "08"
    extracted = {p.name for p in raw_dir.iterdir()}
    for prefix in REQUIRED_CSV_PREFIXES:
        assert any(name.startswith(prefix) for name in extracted), \
            f"missing {prefix} in extraction"


def test_daily_run_default_date_is_yesterday_in_wib(tmp_path, templates, monkeypatch):
    settings = _settings(tmp_path, templates)
    zip_dir = settings.REPORT_BASE_DIR / "2026-10" / "Raw"
    zip_dir.mkdir(parents=True, exist_ok=True)
    _mk_zip(zip_dir / "08.zip")  # today=10/9 -> report day 10/8

    monkeypatch.setattr(scheduler.Scheduler, "_download_zip",
                        lambda self, p: None)  # skip; zip pre-seeded

    s = scheduler.Scheduler(settings, today=datetime(2026, 10, 9, tzinfo=WIB))
    summary = s.run(mode="daily", skip_download=True)
    assert summary["success"] is True
    assert summary["report_day"] == "2026-10-08"
    raw = settings.REPORT_BASE_DIR / "2026-10" / "Raw" / "08"
    assert raw.is_dir()


def test_monthly_run_uses_previous_month_and_monthly_zip(tmp_path, templates, monkeypatch):
    settings = _settings(tmp_path, templates)
    zip_dir = settings.REPORT_BASE_DIR / "2026-09" / "Raw"
    zip_dir.mkdir(parents=True, exist_ok=True)
    _mk_zip(zip_dir / "Monthly.zip")

    monkeypatch.setattr(scheduler.Scheduler, "_download_zip", lambda self, p: None)
    s = scheduler.Scheduler(settings, today=datetime(2026, 10, 5, tzinfo=WIB))
    summary = s.run(mode="monthly", skip_download=True)
    assert summary["success"] is True
    assert summary["report_day"] == "2026-09-01"  # monthly report day = 1st of the report month
    raw = settings.REPORT_BASE_DIR / "2026-09" / "Raw" / "Monthly"
    assert raw.is_dir()


def test_skip_download_missing_zip_fails(tmp_path, templates, monkeypatch):
    settings = _settings(tmp_path, templates)
    monkeypatch.setattr(scheduler.Scheduler, "_download_zip", lambda self, p: None)
    s = scheduler.Scheduler(settings, today=datetime(2026, 10, 9, tzinfo=WIB))
    summary = s.run(mode="daily", date_override=date(2026, 10, 8), skip_download=True)
    assert summary["success"] is False
    assert summary["error"] is not None
    assert "not found" in summary["error"].lower() or "missing" in summary["error"].lower()


def test_extraction_failure_captured_not_raised(tmp_path, templates, monkeypatch):
    settings = _settings(tmp_path, templates)
    # Pre-seed a zip that lacks required CSVs.
    zip_dir = settings.REPORT_BASE_DIR / "2026-10" / "Raw"
    zip_dir.mkdir(parents=True, exist_ok=True)
    bad = zip_dir / "08.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("#UP_time_Report_Switch_Daily/readme.txt", "nothing")

    monkeypatch.setattr(scheduler.Scheduler, "_download_zip", lambda self, p: None)
    s = scheduler.Scheduler(settings, today=datetime(2026, 10, 9, tzinfo=WIB))
    summary = s.run(mode="daily", date_override=date(2026, 10, 8), skip_download=True)
    assert summary["success"] is False
    assert "avg_uptime_by_any_controller" in (summary["error"] or "")


def test_run_notifies_on_success_and_failure(tmp_path, templates, monkeypatch):
    settings = _settings(tmp_path, templates, gowa=True)
    sent = []

    def fake_notify_start(settings, mode, target_date_str):
        sent.append({"type": "start", "mode": mode, "target_date_str": target_date_str})
        return True

    def fake_notify(settings, success, mode, target_date_str, duration_sec, files_written, error):
        sent.append({
            "type": "result",
            "success": success,
            "mode": mode,
            "target_date_str": target_date_str,
            "duration_sec": duration_sec,
            "files_written": files_written,
            "error": error,
        })
        return True

    # Patch the `notifier` module object the scheduler imports.
    import types
    monkeypatch.setattr(scheduler, "notifier",
                        types.SimpleNamespace(notify_start=fake_notify_start, notify=fake_notify))

    zip_dir = settings.REPORT_BASE_DIR / "2026-10" / "Raw"
    zip_dir.mkdir(parents=True, exist_ok=True)
    _mk_zip(zip_dir / "08.zip")
    monkeypatch.setattr(scheduler.Scheduler, "_download_zip", lambda self, p: None)

    s = scheduler.Scheduler(settings, today=datetime(2026, 10, 9, tzinfo=WIB))
    ok = s.run(mode="daily", date_override=date(2026, 10, 8), skip_download=True)
    assert ok["success"] is True
    # Check START notification was sent
    assert any(kw.get("type") == "start" and kw.get("success") is None for kw in sent), "START notification missing"
    # Check SUCCESS notification was sent
    assert any(kw.get("type") == "result" and kw.get("success") is True for kw in sent), "success notification missing"

    # Now force a failure (bad zip) -> failure notification.
    (zip_dir / "08.zip").write_bytes(b"junk")  # not a valid zip
    sent.clear()
    bad = s.run(mode="daily", date_override=date(2026, 10, 8), skip_download=True)
    assert bad["success"] is False
    # Check START notification was sent
    assert any(kw.get("type") == "start" for kw in sent), "START notification missing on failure"
    # Check FAILED notification was sent
    assert any(kw.get("type") == "result" and kw.get("success") is False for kw in sent), "failure notification missing"


def test_bad_mode_rejected(tmp_path, templates):
    settings = _settings(tmp_path, templates)
    s = scheduler.Scheduler(settings, today=datetime(2026, 10, 9, tzinfo=WIB))
    with pytest.raises(ValueError):
        s.run(mode="weekly", skip_download=True)


def test_main_cli_args(tmp_path, templates, monkeypatch):
    settings = _settings(tmp_path, templates)

    zip_dir = settings.REPORT_BASE_DIR / "2026-10" / "Raw"
    zip_dir.mkdir(parents=True, exist_ok=True)
    _mk_zip(zip_dir / "08.zip")
    monkeypatch.setattr(scheduler.Scheduler, "_download_zip", lambda self, p: None)

    # Patch Settings so main() uses the prebuilt tmp settings (no .env load).
    monkeypatch.setattr(scheduler, "Settings", lambda: settings)

    # Pin "today" so the CLI's date default doesn't drift between runs.
    real_init = scheduler.Scheduler.__init__

    def pinned_init(self, settings):
        real_init(self, settings, today=datetime(2026, 10, 9, tzinfo=WIB))

    monkeypatch.setattr(scheduler.Scheduler, "__init__", pinned_init)

    rc = scheduler.main(["--mode", "daily", "--date", "2026-10-08",
                         "--skip-download"])
    assert rc == 0


def test_main_cli_bad_date_rejected(tmp_path, templates, monkeypatch):
    settings = _settings(tmp_path, templates)
    monkeypatch.setattr(scheduler, "Settings", lambda: settings)
    rc = scheduler.main(["--mode", "daily", "--date", "2026-13-40",
                         "--skip-download"])
    assert rc != 0  # argparse / ValueError surfaced as exit 1


def test_main_cli_bad_mode_rejected(tmp_path, templates, monkeypatch):
    settings = _settings(tmp_path, templates)
    monkeypatch.setattr(scheduler, "Settings", lambda: settings)
    rc = scheduler.main(["--mode", "weekly", "--date", "2026-10-08",
                         "--skip-download"])
    assert rc != 0