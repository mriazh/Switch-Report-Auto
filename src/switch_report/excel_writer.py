"""OpenPyXL writer that populates the 8 switch-report workbooks (FR-4).

The module reads the 9 raw CSVs produced by `archive_extractor`, transforms
cell text (percent -> float, strip tabs/quotes, keep the "-" placeholder),
clones the target sheet (`<DD>` or `Monthly`) from `Master` / prior day,
clears the data region, and writes the fresh rows idempotently. Missing
output workbooks are auto-seeded from `config/templates/`.
"""
from __future__ import annotations

import csv
import shutil
from pathlib import Path
from typing import Iterable, Sequence

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

# 8 target workbooks, in canonical order (design.md 2.4).
WORKBOOK_NAMES: tuple[str, ...] = (
    "Avg Uptime By Any Controller.xlsx",
    "Avg Uptime By Any Switch.xlsx",
    "Avg Uptime by Device.xlsx",
    "Most Utilization Switch.xlsx",
    "Most Utilization By Usage.xlsx",
    "Top Utilization CPU dan Memory.xlsx",
    "Daily Monitor Up Time Switch All GMF CGK HO & OSA.xlsx",
    "Device Switch Downtime Reebort Detail.xlsx",
)

# Workbook filename -> the raw CSV category keys it consumes.
WORKBOOK_CSV_REFS: dict[str, tuple[str, ...]] = {
    "Avg Uptime By Any Controller.xlsx": ("avg_uptime_by_any_controller",),
    "Avg Uptime By Any Switch.xlsx": ("avg_uptime_by_any_switch",),
    "Avg Uptime by Device.xlsx": ("avg_uptime_by_device",),
    "Most Utilization Switch.xlsx": ("most_utilized_switches",),
    "Most Utilization By Usage.xlsx": ("most_utilized_by_usage",),
    "Top Utilization CPU dan Memory.xlsx": (
        "top_cpu_utilization_by_device",
        "top_memory_usage_by_device",
    ),
    "Daily Monitor Up Time Switch All GMF CGK HO & OSA.xlsx": ("uptime_by_device",),
    "Device Switch Downtime Reebort Detail.xlsx": ("device_downtime_detail",),
}

# Low < 35% (strict), medium 35-65% (35 <= x < 65), high >= 65%.
_LOW = 0.35
_MEDIUM = 0.65


# ---------------------------------------------------------------------------
# Cell cleaning / transformation helpers
# ---------------------------------------------------------------------------


def clean_cell(value) -> object:
    """Normalize a raw CSV cell.

    - Percent strings ("100.00%", "43.47%") become floats (1.0, 0.4347).
    - Tab/quote-wrapped placeholders ("\\t-") become the bare string "-".
    - Whitespace/tabs and surrounding quotes are stripped from text.
    - Non-string scalars (None, int, float) pass through untouched.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return value

    s = value.strip().strip('"').strip()
    if s.endswith("%"):
        num = s[:-1].strip()
        if num in {"", "-", "	-"}:
            return "-"
        try:
            return round(float(num) / 100.0, 10)
        except ValueError:
            return s
    if s in {"", "-"}:
        return s
    # A tab-prefixed dash placeholder.
    if s == "\t-" or s.replace("\t", "") == "-":
        return "-"
    return s


def classify_utilization(frac: float) -> str:
    """Return 'low' | 'medium' | 'high' for a 0-1 utilization fraction."""
    if frac >= _MEDIUM:
        return "high"
    if frac >= _LOW:
        return "medium"
    return "low"


def _tick_columns_for_percent(frac: float, medium_col: int, high_col: int) -> int:
    """Return the 1-indexed column that carries the '✓' for a percent band.

    Tick-band mapping (per the workbook legend): low <35%, medium 35-65%
    (closed at 65%), high >65%. Note the 65% boundary belongs to the medium
    tick band; `classify_utilization` uses the >=65% convention and is a
    separate helper.

    low -> the column *before* medium_col (i.e. medium_col - 1).
    """
    if frac > _MEDIUM:
        return high_col
    if frac >= _LOW:
        return medium_col
    return medium_col - 1


# ---------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------


def _find_csv(raw_dir: Path, key: str) -> Path:
    matches = sorted(raw_dir.glob(f"{key}_*.csv"))
    if not matches:
        raise ValueError(f"required raw CSV not found for key '{key}' in {raw_dir}")
    return matches[0]


def _read_rows(raw_dir: Path, key: str) -> list[dict[str, str]]:
    """Read one CSV into a list of dict rows keyed by header column name."""
    path = _find_csv(raw_dir, key)
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        return [dict(r) for r in reader]


def _top(rows: list[dict], limit: int) -> list[dict]:
    return rows[:limit]


# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------


def _ensure_sheet(wb, sheet: str, daily: bool) -> None:
    """Ensure `sheet` exists, cloning from a prior day or Master."""
    if sheet in wb.sheetnames:
        return
    if daily and sheet.isdigit():
        prev = f"{int(sheet) - 1:02d}"
        source = prev if prev in wb.sheetnames else "Master"
    else:
        source = "Master"
    src_ws = wb[source]
    new_ws = wb.copy_worksheet(src_ws)
    new_ws.title = sheet
    wb.active = 0


def _clear_rows(ws, first_row: int, last_row: int, ncols: int) -> None:
    """Blank out the data region of the target sheet."""
    for r in range(first_row, last_row + 1):
        for c in range(1, ncols + 1):
            ws.cell(row=r, column=c).value = None


# ---------------------------------------------------------------------------
# Per-workbook writers (each returns the target sheet name it wrote to)
# ---------------------------------------------------------------------------


def _w_avg_controller(ws, raw_dir) -> None:
    rows = _read_rows(raw_dir, "avg_uptime_by_any_controller")
    row = rows[0] if rows else {}
    _clear_rows(ws, 5, 5, 4)
    vals = [row.get(h, "") for h in
            ("Device Uptime", "SNMP Comm Uptime", "ICMP Comm Uptime", "HTTPS Comm Uptime")]
    for c, v in enumerate(vals, start=1):
        ws.cell(row=5, column=c).value = clean_cell(v)


def _w_avg_switch(ws, raw_dir) -> None:
    rows = _read_rows(raw_dir, "avg_uptime_by_any_switch")
    row = rows[0] if rows else {}
    _clear_rows(ws, 6, 6, 4)
    vals = [row.get(h, "") for h in
            ("Device Uptime", "SNMP Comm Uptime", "ICMP Comm Uptime", "HTTPS Comm Uptime")]
    for c, v in enumerate(vals, start=1):
        ws.cell(row=6, column=c).value = clean_cell(v)


def _w_avg_device(ws, raw_dir) -> None:
    rows = _read_rows(raw_dir, "avg_uptime_by_device")
    row = rows[0] if rows else {}
    _clear_rows(ws, 5, 5, 4)
    vals = [row.get(h, "") for h in
            ("Device Uptime", "SNMP Comm Uptime", "ICMP Comm Uptime", "HTTPS Comm Uptime")]
    for c, v in enumerate(vals, start=1):
        ws.cell(row=5, column=c).value = clean_cell(v)


def _w_most_switch(ws, raw_dir) -> None:
    rows = _top(_read_rows(raw_dir, "most_utilized_switches"), 10)
    _clear_rows(ws, 5, 14, 5)
    for i, row in enumerate(rows):
        r = 5 + i
        ws.cell(row=r, column=2).value = i + 1
        ws.cell(row=r, column=3).value = clean_cell(row.get("Device", ""))
        ws.cell(row=r, column=4).value = clean_cell(row.get("Total Traffic", ""))
        ws.cell(row=r, column=5).value = clean_cell(row.get("Remark", ""))


def _w_most_usage(ws, raw_dir) -> None:
    rows = _top(_read_rows(raw_dir, "most_utilized_by_usage"), 10)
    _clear_rows(ws, 6, 15, 9)
    cols = ("Device", "Max Clients", "Unique Clients", "Total Data",
            "Avg Usage", "Location", "Controller", "Folder", "Group")
    for i, row in enumerate(rows):
        r = 6 + i
        ws.cell(row=r, column=1).value = i + 1
        for c, h in enumerate(cols, start=2):
            ws.cell(row=r, column=c).value = clean_cell(row.get(h, ""))


def _w_top_cpu_mem(ws, raw_dir) -> None:
    cpu = _top(_read_rows(raw_dir, "top_cpu_utilization_by_device"), 10)
    mem = _top(_read_rows(raw_dir, "top_memory_usage_by_device"), 10)
    # Data rows 7-16. CPU block cols 1-8, Memory block cols 9-16.
    _clear_rows(ws, 7, 16, 16)
    for i in range(10):
        r = 7 + i
        c_row = cpu[i] if i < len(cpu) else {}
        m_row = mem[i] if i < len(mem) else {}
        cpu_frac = clean_cell(c_row.get("CPU Utilization %", "0%"))
        cpu_frac = cpu_frac if isinstance(cpu_frac, (int, float)) else 0.0
        mem_frac = clean_cell(m_row.get("Memory Utilization %", "0%"))
        mem_frac = mem_frac if isinstance(mem_frac, (int, float)) else 0.0

        def _block(no, dev, util, pct, low_col, med_col, high_col, frac, dev_txt, util_txt):
            ws.cell(row=r, column=no).value = i + 1
            ws.cell(row=r, column=dev).value = clean_cell(dev_txt)
            ws.cell(row=r, column=util).value = clean_cell(util_txt)
            ws.cell(row=r, column=pct).value = frac
            ws.cell(row=r, column=_tick_columns_for_percent(frac, med_col, high_col)).value = "✓"

        # CPU: No=2, Device=3, Utilization=4, CPU%=5, low=6, med=7, high=8
        _block(2, 3, 4, 5, 6, 7, 8, cpu_frac,
               c_row.get("Device", ""), c_row.get("Utilization", ""))
        # Memory: No=10, Device=11, Utilization=12, Mem%=13, low=14, med=15, high=16
        _block(10, 11, 12, 13, 14, 15, 16, mem_frac,
               m_row.get("Device", ""), m_row.get("Utilization", ""))


def _w_daily_monitor(ws, raw_dir) -> None:
    rows = _read_rows(raw_dir, "uptime_by_device")
    cols = ("Device", "Group", "Folder", "Device Uptime", "Device Uptime is Reliable",
            "Time Since Last Contact", "Time Since Last Boot", "SNMP Comm Uptime",
            "SNMP Comm Up/Down Times", "ICMP Comm Uptime", "ICMP Comm Up/Down Times",
            "HTTPS Comm Uptime", "HTTPS Comm Up/Down Times")
    _clear_rows(ws, 5, max(ws.max_row, 4 + len(rows)), 14)
    for i, row in enumerate(rows):
        r = 5 + i
        for c, h in enumerate(cols, start=2):  # col 1 = No already present
            ws.cell(row=r, column=c).value = clean_cell(row.get(h, ""))


def _w_downtime(ws, raw_dir) -> None:
    rows = _read_rows(raw_dir, "device_downtime_detail")
    cols = ("Device", "Group", "Folder", "Bootstrap Count", "Reboot Count",
            "Device Down Duration", "Device Downtime Duration (%)",
            "Comm Down Duration", "Comm Down Duration (%)")
    _clear_rows(ws, 5, max(ws.max_row, 4 + len(rows)), 10)
    for i, row in enumerate(rows):
        r = 5 + i
        for c, h in enumerate(cols, start=2):
            ws.cell(row=r, column=c).value = clean_cell(row.get(h, ""))


_WRITERS = {
    "Avg Uptime By Any Controller.xlsx": _w_avg_controller,
    "Avg Uptime By Any Switch.xlsx": _w_avg_switch,
    "Avg Uptime by Device.xlsx": _w_avg_device,
    "Most Utilization Switch.xlsx": _w_most_switch,
    "Most Utilization By Usage.xlsx": _w_most_usage,
    "Top Utilization CPU dan Memory.xlsx": _w_top_cpu_mem,
    "Daily Monitor Up Time Switch All GMF CGK HO & OSA.xlsx": _w_daily_monitor,
    "Device Switch Downtime Reebort Detail.xlsx": _w_downtime,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_workbook(name: str, sheet: str, output_dir: Path, raw_dir: Path,
                   templates_dir: Path, mode: str = "daily") -> Path:
    """Populate one workbook's `sheet` and save to `output_dir/name`.

    `sheet` is `<DD>` for daily or `Monthly` for monthly. A missing output
    workbook is seeded from `templates_dir/name`. Idempotent: the data region
    is cleared before writing so a repeat run replaces, not duplicates.
    """
    out_path = Path(output_dir) / name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not out_path.exists():
        tpl = Path(templates_dir) / name
        if not tpl.exists():
            raise FileNotFoundError(f"template not found for '{name}': {tpl}")
        shutil.copyfile(tpl, out_path)

    wb = load_workbook(out_path)
    _ensure_sheet(wb, sheet, daily=(mode == "daily"))
    ws = wb[sheet]
    _WRITERS[name](ws, Path(raw_dir))
    wb.save(out_path)
    return out_path


def run_all(output_dir: Path, raw_dir: Path, sheet: str, mode: str = "daily",
            templates_dir: Path | None = None) -> list[Path]:
    """Populate every one of the 8 workbooks; returns the saved paths."""
    templates_dir = Path(templates_dir) if templates_dir else None
    out: list[Path] = []
    for name in WORKBOOK_NAMES:
        out.append(write_workbook(name, sheet, output_dir, raw_dir,
                                  templates_dir, mode))
    return out
