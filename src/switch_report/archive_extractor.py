"""Extract and validate AirWave raw CSV zip archives.

AirWave's report 681 ships a ZIP whose entries are wrapped in a folder
(`#UP_time_Report_Switch_Daily/`). The extractor flattens any nesting so the
9 mandatory CSVs land directly under the target directory, then validates that
every required category is present before returning a key -> Path map.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

# The 9 canonical categories (design.md 2.3 / requirements FR-3).
# Each maps to any file whose stem starts with `<key>_<timestamp>`.
REQUIRED_CSV_PREFIXES: tuple[str, ...] = (
    "avg_uptime_by_any_controller",
    "avg_uptime_by_any_switch",
    "avg_uptime_by_device",
    "device_downtime_detail",
    "most_utilized_by_usage",
    "most_utilized_switches",
    "top_cpu_utilization_by_device",
    "top_memory_usage_by_device",
    "uptime_by_device",
)


def _match_key(filename: str) -> str | None:
    """Return the canonical key if `filename` starts with `<key>_` else None."""
    for key in REQUIRED_CSV_PREFIXES:
        # Match at the underscore boundary so e.g. 'most_utilized_switches_'
        # does not also claim 'most_utilized_by_usage'.
        if filename.startswith(key + "_") and filename.endswith(".csv"):
            return key
    return None


def extract_archive(zip_path: Path, target_dir: Path) -> dict[str, Path]:
    """Unzip `zip_path` into `target_dir` and map the 9 required CSVs.

    Nested entry folders are flattened: only the basename of each archive
    entry is used, so `#UP_time_Report_Switch_Daily/uptime_by_device_X.csv`
    becomes `target_dir/uptime_by_device_X.csv`.

    Raises ValueError if any required CSV category is missing, naming every
    missing category so callers can trigger a download retry.
    """
    zip_path = Path(zip_path)
    target_dir = Path(target_dir)
    if not zip_path.is_file():
        raise FileNotFoundError(f"zip archive not found: {zip_path}")

    target_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # Flatten: ignore any directory the entry is nested under.
            dest = target_dir / Path(info.filename).name
            with zf.open(info) as src, open(dest, "wb") as dst:
                dst.write(src.read())

    found: dict[str, Path] = {}
    for entry in target_dir.iterdir():
        if not entry.is_file():
            continue
        key = _match_key(entry.name)
        if key is not None and key not in found:
            found[key] = entry

    missing = [k for k in REQUIRED_CSV_PREFIXES if k not in found]
    if missing:
        raise ValueError(
            "Required CSV(s) missing from archive "
            f"'{zip_path.name}': {', '.join(missing)}. "
            "Trigger a fresh AirWave download and retry."
        )
    return found
