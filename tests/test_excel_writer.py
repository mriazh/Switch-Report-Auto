"""TDD tests for switch_report.excel_writer (design.md 2.4 / FR-4)."""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from switch_report import excel_writer as ew

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The 9 canonical keys produced by the archive extractor.
ALL_KEYS = ew.WORKBOOK_CSV_REFS  # dict name -> tuple of keys

WB_NAMES = ew.WORKBOOK_NAMES  # list of 8 filenames


def _clean_cell(x):
    return ew.clean_cell(x)


# ---------------------------------------------------------------------------
# clean_cell / percentage unit tests
# ---------------------------------------------------------------------------


def test_clean_cell_percent_to_float():
    assert _clean_cell("100.00%") == 1.0
    assert _clean_cell("0.00%") == 0.0
    assert _clean_cell(" 43.47% ") == 0.4347
    assert _clean_cell("98.62%") == 0.9862


def test_clean_cell_dash_placeholder():
    assert _clean_cell("	-") == "-"
    assert _clean_cell("-") == "-"
    assert _clean_cell("") == ""


def test_clean_cell_strips_tabs_and_quotes():
    assert _clean_cell('\tASW01\t') == "ASW01"
    assert _clean_cell('"SW Hangar 4"') == "SW Hangar 4"
    assert _clean_cell("	-") == "-"  # dash keeps its meaning, no stray tab


def test_clean_cell_numeric_passthrough():
    assert _clean_cell(10) == 10
    assert _clean_cell(1.5) == 1.5
    assert _clean_cell(None) is None
    assert _clean_cell("plain text") == "plain text"


# ---------------------------------------------------------------------------
# Fixtures: minimal 8-workbook seed + a raw CSV dir
# ---------------------------------------------------------------------------


def _mk_workbook(path: Path) -> None:
    """Seed a minimal workbook with Master + 01..05 + Monthly sheets."""
    wb = Workbook()
    master = wb.active
    master.title = "Master"
    # header-ish placeholder in Master
    master["A1"] = "Master"
    for day in ("01", "02", "03", "04", "05"):
        ws = wb.create_sheet(day)
        ws["A1"] = day
    m = wb.create_sheet("Monthly")
    m["A1"] = "Monthly"
    wb.save(path)


@pytest.fixture
def templates_dir(tmp_path: Path) -> Path:
    tdir = tmp_path / "templates"
    tdir.mkdir()
    for name in WB_NAMES:
        _mk_workbook(tdir / name)
    return tdir


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


@pytest.fixture
def raw_dir(tmp_path: Path) -> Path:
    """Build a raw dir containing all 9 required CSVs with sample data."""
    d = tmp_path / "raw"
    d.mkdir()
    ts = "12345"

    _write_csv(d / f"avg_uptime_by_any_controller_{ts}.csv",
               ["Device Uptime", "SNMP Comm Uptime", "ICMP Comm Uptime", "HTTPS Comm Uptime"],
               [["98.62%", "98.64%", "99.32%", "	-"]])

    _write_csv(d / f"avg_uptime_by_any_switch_{ts}.csv",
               ["Device Uptime", "SNMP Comm Uptime", "ICMP Comm Uptime", "HTTPS Comm Uptime"],
               [["100.00%", "0.00%", "55.00%", "-"]])

    _write_csv(d / f"avg_uptime_by_device_{ts}.csv",
               ["Device Uptime", "SNMP Comm Uptime", "ICMP Comm Uptime", "HTTPS Comm Uptime"],
               [["99.00%", "99.50%", "98.00%", "	-"]])

    _write_csv(d / f"most_utilized_switches_{ts}.csv",
               ["Device", "Total Traffic"],
               [[f"DEV{i:02d}", "1.00 TiB"] for i in range(1, 13)])  # 12 rows -> top10

    _write_csv(d / f"most_utilized_by_usage_{ts}.csv",
               ["Device", "Max Clients", "Unique Clients", "Total Data", "Avg Usage",
                "Location", "Controller", "Folder", "Group"],
               [[f"DEV{i:02d}", "10", "5", "2.0 GiB", "43.47%",
                 "Loc", "Ctrl", "Folder", "Grp"] for i in range(1, 13)])

    _write_csv(d / f"top_cpu_utilization_by_device_{ts}.csv",
               ["Device", "Utilization", "CPU Utilization %"],
               [[f"DEV{i:02d}", "50%", "20.00%"] for i in range(1, 13)])

    _write_csv(d / f"top_memory_usage_by_device_{ts}.csv",
               ["Device", "Utilization", "Memory Utilization %"],
               [[f"DEV{i:02d}", "70%", "65.00%"] for i in range(1, 13)])

    _write_csv(d / f"uptime_by_device_{ts}.csv",
               ["Device", "Group", "Folder", "Device Uptime", "Device Uptime is Reliable",
                "Time Since Last Contact", "Time Since Last Boot", "SNMP Comm Uptime",
                "SNMP Comm Up/Down Times", "ICMP Comm Uptime", "ICMP Comm Up/Down Times",
                "HTTPS Comm Uptime", "HTTPS Comm Up/Down Times"],
               [[f"SW{i:02d}", "Grp", "Top", "100.00%", "Yes", "ok", "boot",
                 "100.00%", "0", "100.00%", "0", "	-", "	-"] for i in range(1, 6)])

    _write_csv(d / f"device_downtime_detail_{ts}.csv",
               ["Device", "Group", "Folder", "Bootstrap Count", "Reboot Count",
                "Device Down Duration", "Device Downtime Duration (%)",
                "Comm Down Duration", "Comm Down Duration (%)"],
               [[f"SW{i:02d}", "Grp", "Top", "	-", "	-", "0 secs", "0.00%",
                 "0 secs", "0.00%"] for i in range(1, 4)])
    return d


# ---------------------------------------------------------------------------
# Auto-seeding
# ---------------------------------------------------------------------------


def test_write_seeds_missing_workbook_from_templates(templates_dir, raw_dir, tmp_path):
    out = tmp_path / "Output"
    ew.write_workbook("Avg Uptime by Device.xlsx", "05", out, raw_dir,
                       templates_dir=templates_dir)
    assert (out / "Avg Uptime by Device.xlsx").is_file()


def test_write_reuses_existing_output_workbook(templates_dir, raw_dir, tmp_path):
    out = tmp_path / "Output"
    out.mkdir()
    # Pre-create the output workbook so we exercise the "already exists" branch.
    shutil.copy(templates_dir / "Avg Uptime by Device.xlsx", out / "Avg Uptime by Device.xlsx")
    ew.write_workbook("Avg Uptime by Device.xlsx", "05", out, raw_dir,
                      templates_dir=templates_dir)
    wb = load_workbook(out / "Avg Uptime by Device.xlsx")
    # Data was written on row 5.
    assert wb["05"]["A5"].value == 0.99


def test_seed_fails_clearly_if_template_missing(templates_dir, raw_dir, tmp_path):
    (templates_dir / "Avg Uptime by Device.xlsx").unlink()
    with pytest.raises(FileNotFoundError):
        ew.write_workbook("Avg Uptime by Device.xlsx", "05", tmp_path / "Output", raw_dir,
                          templates_dir=templates_dir)


# ---------------------------------------------------------------------------
# Sheet cloning + daily injection
# ---------------------------------------------------------------------------


def test_daily_injection_creates_and_writes_sheet(templates_dir, raw_dir, tmp_path):
    out = tmp_path / "Output"
    ew.write_workbook("Avg Uptime by Device.xlsx", "07", out, raw_dir,
                      templates_dir=templates_dir)
    wb = load_workbook(out / "Avg Uptime by Device.xlsx")
    assert "07" in wb.sheetnames
    # Row 5, cols 1-4 -> 4 floats
    row5 = [wb["07"].cell(row=5, column=c).value for c in range(1, 5)]
    assert row5 == [0.99, 0.995, 0.98, "-"]


def test_daily_injection_idempotent_overwrites(templates_dir, raw_dir, tmp_path):
    out = tmp_path / "Output"
    ew.write_workbook("Avg Uptime by Device.xlsx", "05", out, raw_dir,
                      templates_dir=templates_dir)
    # Run again on same sheet -> data replaced, not duplicated.
    ew.write_workbook("Avg Uptime by Device.xlsx", "05", out, raw_dir,
                      templates_dir=templates_dir)
    wb = load_workbook(out / "Avg Uptime by Device.xlsx")
    row5 = [wb["05"].cell(row=5, column=c).value for c in range(1, 5)]
    assert row5 == [0.99, 0.995, 0.98, "-"]
    # No accidental second copy of the sheet.
    assert wb.sheetnames.count("05") == 1


def test_monthly_injection_writes_to_monthly_sheet(templates_dir, raw_dir, tmp_path):
    out = tmp_path / "Output"
    ew.write_workbook("Avg Uptime by Device.xlsx", "Monthly", out, raw_dir,
                      templates_dir=templates_dir, mode="monthly")
    wb = load_workbook(out / "Avg Uptime by Device.xlsx")
    assert "Monthly" in wb.sheetnames
    row5 = [wb["Monthly"].cell(row=5, column=c).value for c in range(1, 5)]
    assert row5 == [0.99, 0.995, 0.98, "-"]


# ---------------------------------------------------------------------------
# Workbook 2: single row at row 6
# ---------------------------------------------------------------------------


def test_avg_uptime_by_any_switch_row6(templates_dir, raw_dir, tmp_path):
    out = tmp_path / "Output"
    ew.write_workbook("Avg Uptime By Any Switch.xlsx", "05", out, raw_dir,
                      templates_dir=templates_dir)
    wb = load_workbook(out / "Avg Uptime By Any Switch.xlsx")
    row6 = [wb["05"].cell(row=6, column=c).value for c in range(1, 5)]
    assert row6 == [1.0, 0.0, 0.55, "-"]


# ---------------------------------------------------------------------------
# Workbook 4: Most Utilization Switch, rows 5-14 top10
# ---------------------------------------------------------------------------


def test_most_utilization_switch_top10(templates_dir, raw_dir, tmp_path):
    out = tmp_path / "Output"
    ew.write_workbook("Most Utilization Switch.xlsx", "05", out, raw_dir,
                      templates_dir=templates_dir)
    wb = load_workbook(out / "Most Utilization Switch.xlsx")
    ws = wb["05"]
    # 12 device rows in CSV -> only top 10 written (rows 5-14).
    assert ws.cell(row=5, column=2).value == 1          # No
    assert ws.cell(row=5, column=3).value == "DEV01"    # Device
    assert ws.cell(row=14, column=3).value == "DEV10"
    # Row 15 (11th device) must be cleared / absent.
    assert ws.cell(row=15, column=3).value in (None, "")


# ---------------------------------------------------------------------------
# Workbook 5: Most Utilization By Usage, rows 6-15 top10, cols 1-9
# ---------------------------------------------------------------------------


def test_most_utilization_by_usage(templates_dir, raw_dir, tmp_path):
    out = tmp_path / "Output"
    ew.write_workbook("Most Utilization By Usage.xlsx", "05", out, raw_dir,
                      templates_dir=templates_dir)
    wb = load_workbook(out / "Most Utilization By Usage.xlsx")
    ws = wb["05"]
    # Rank/No col A, device col B, avg usage is a percent -> float in col F.
    assert ws.cell(row=6, column=1).value == 1
    assert ws.cell(row=6, column=2).value == "DEV01"
    assert ws.cell(row=15, column=2).value == "DEV10"
    assert ws.cell(row=15, column=6).value == 0.4347  # Avg Usage 43.47%


# ---------------------------------------------------------------------------
# Workbook 6: CPU & Memory top10 with check marks <35 / 35-65 / >=65
# ---------------------------------------------------------------------------


def test_top_utilization_checkmarks(templates_dir, raw_dir, tmp_path):
    out = tmp_path / "Output"
    ew.write_workbook("Top Utilization CPU dan Memory.xlsx", "05", out, raw_dir,
                      templates_dir=templates_dir)
    wb = load_workbook(out / "Top Utilization CPU dan Memory.xlsx")
    ws = wb["05"]
    # First CPU row is at row 7. CPU% col5 = 20.00% -> low -> tick in col6.
    assert ws.cell(row=7, column=5).value == 0.2
    assert ws.cell(row=7, column=6).value == "✓"          # low tick
    # Memory% col13 = 65.00% -> medium/high boundary -> tick in col15 (medium).
    assert ws.cell(row=7, column=13).value == 0.65
    assert ws.cell(row=7, column=15).value == "✓"
    # The two non-active tick cells stay empty.
    assert ws.cell(row=7, column=7).value in (None, "")
    assert ws.cell(row=7, column=16).value in (None, "")


def test_top_utilization_medium_band(templates_dir, raw_dir, tmp_path):
    # 40% lands in the medium band -> tick in the medium column.
    assert ew.classify_utilization(0.40) == "medium"
    assert ew.classify_utilization(0.20) == "low"
    assert ew.classify_utilization(0.65) == "high"
    assert ew.classify_utilization(0.35) == "medium"
    assert ew.classify_utilization(0.34) == "low"

    # The tick-band helper uses its own convention: 65% is the closed edge of
    # the medium tick band (medium 35-65%), so 0.65 still marks the medium col.
    assert ew._tick_columns_for_percent(0.20, medium_col=7, high_col=8) == 6   # low
    assert ew._tick_columns_for_percent(0.40, medium_col=7, high_col=8) == 7   # medium
    assert ew._tick_columns_for_percent(0.65, medium_col=7, high_col=8) == 7   # medium (closed edge)
    assert ew._tick_columns_for_percent(0.70, medium_col=7, high_col=8) == 8   # high


# ---------------------------------------------------------------------------
# Workbooks 7 & 8: multi-row, clear trailing
# ---------------------------------------------------------------------------


def test_daily_monitor_writes_device_rows(templates_dir, raw_dir, tmp_path):
    out = tmp_path / "Output"
    ew.write_workbook("Daily Monitor Up Time Switch All GMF CGK HO & OSA.xlsx",
                      "05", out, raw_dir, templates_dir=templates_dir)
    wb = load_workbook(out / "Daily Monitor Up Time Switch All GMF CGK HO & OSA.xlsx")
    ws = wb["05"]
    # 5 devices fill rows 5-9; the next row (10) must be cleared.
    assert ws.cell(row=5, column=2).value == "SW01"
    assert ws.cell(row=5, column=5).value == 1.0
    assert ws.cell(row=9, column=2).value == "SW05"  # last device row
    assert ws.cell(row=10, column=2).value in (None, "")  # beyond 5 devices


def test_device_downtime_writes_and_clears_trailing(templates_dir, raw_dir, tmp_path):
    out = tmp_path / "Output"
    ew.write_workbook("Device Switch Downtime Reebort Detail.xlsx",
                      "05", out, raw_dir, templates_dir=templates_dir)
    wb = load_workbook(out / "Device Switch Downtime Reebort Detail.xlsx")
    ws = wb["05"]
    assert ws.cell(row=5, column=2).value == "SW01"
    assert ws.cell(row=7, column=2).value == "SW03"
    assert ws.cell(row=8, column=2).value in (None, "")


def test_workbooks_7_8_clear_rows_beyond_new_count(templates_dir, raw_dir, tmp_path):
    """A run with MORE devices than the sheet previously held must not leave stale rows."""
    out = tmp_path / "Output"
    # 1) first run with 3 devices
    ew.write_workbook("Device Switch Downtime Reebort Detail.xlsx",
                      "05", out, raw_dir, templates_dir=templates_dir)
    # 2) grow the CSV to 5 devices and re-run the same sheet.
    for f in raw_dir.glob("device_downtime_detail_*.csv"):
        with open(f, "a", newline="") as fh:
            csv.writer(fh).writerows([
                ["SW04", "Grp", "Top", "\t-", "\t-", "0 secs", "0.00%", "0 secs", "0.00%"],
                ["SW05", "Grp", "Top", "\t-", "\t-", "0 secs", "0.00%", "0 secs", "0.00%"],
            ])
    ew.write_workbook("Device Switch Downtime Reebort Detail.xlsx",
                      "05", out, raw_dir, templates_dir=templates_dir)
    ws = load_workbook(out / "Device Switch Downtime Reebort Detail.xlsx")["05"]
    # 5 devices now occupy rows 5-9.
    assert ws.cell(row=9, column=2).value == "SW05"
    assert ws.cell(row=10, column=2).value in (None, "")


# ---------------------------------------------------------------------------
# run_all (one call populates every workbook)
# ---------------------------------------------------------------------------


def test_run_all_populates_every_workbook(templates_dir, raw_dir, tmp_path):
    out = tmp_path / "Output"
    written = ew.run_all(out, raw_dir, "05", mode="daily",
                          templates_dir=templates_dir)
    assert len(written) == 8
    for name in WB_NAMES:
        assert (out / name).is_file()
