"""TDD tests for switch_report.archive_extractor."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from switch_report.archive_extractor import REQUIRED_CSV_PREFIXES, extract_archive

# Canonical keys -> the 9 required prefixes (see design.md 2.3).
EXPECTED_KEYS = [
    "avg_uptime_by_any_controller",
    "avg_uptime_by_any_switch",
    "avg_uptime_by_device",
    "device_downtime_detail",
    "most_utilized_by_usage",
    "most_utilized_switches",
    "top_cpu_utilization_by_device",
    "top_memory_usage_by_device",
    "uptime_by_device",
]


def _zip_with_csvs(path: Path, names: list[str], nested: bool = False) -> None:
    """Create a zip. If nested, wrap every entry in the AirWave folder."""
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            arcname = f"#UP_time_Report_Switch_Daily/{name}" if nested else name
            zf.writestr(arcname, "data\n")


def _all_nine(nested: bool = False, timestamp: str = "1788335052") -> list[str]:
    return [f"{key}_{timestamp}.csv" for key in EXPECTED_KEYS] if not nested \
        else [f"{key}_{timestamp}.csv" for key in EXPECTED_KEYS]


def test_extract_flat_zip_returns_all_nine(tmp_path: Path) -> None:
    src = tmp_path / "day.zip"
    _zip_with_csvs(src, _all_nine(nested=False), nested=False)
    out = tmp_path / "out"
    result = extract_archive(src, out)
    assert isinstance(result, dict)
    assert set(result.keys()) == set(EXPECTED_KEYS)
    for key in EXPECTED_KEYS:
        p = result[key]
        assert p.is_file(), f"{key} not extracted"
        assert p.parent == out, f"{key} not flattened into target dir"
        assert p.name.startswith(f"{key}_"), p.name
        assert p.suffix == ".csv"


def test_extract_nested_zip_flattens_folder(tmp_path: Path) -> None:
    """AirWave wraps files in '#UP_time_Report_Switch_Daily/' — extractor must flatten."""
    src = tmp_path / "day.zip"
    _zip_with_csvs(src, _all_nine(nested=True), nested=True)
    out = tmp_path / "out"
    result = extract_archive(src, out)
    assert set(result.keys()) == set(EXPECTED_KEYS)
    for key in EXPECTED_KEYS:
        assert result[key].parent == out
        assert result[key].is_file()


def test_missing_csv_raises_value_error(tmp_path: Path) -> None:
    src = tmp_path / "bad.zip"
    names = [f"{k}_{1}.csv" for k in EXPECTED_KEYS]
    del names[2]  # drop avg_uptime_by_device
    _zip_with_csvs(src, names, nested=False)
    out = tmp_path / "out"
    with pytest.raises(ValueError) as exc:
        extract_archive(src, out)
    msg = str(exc.value)
    assert "avg_uptime_by_device" in msg, f"error should name the missing prefix, got: {msg}"
    assert "missing" in msg.lower() or "not found" in msg.lower() or "required" in msg.lower()


def test_missing_multiple_csvs_lists_all(tmp_path: Path) -> None:
    src = tmp_path / "bad2.zip"
    names = [f"{k}_{1}.csv" for k in EXPECTED_KEYS]
    keep = {EXPECTED_KEYS[0], EXPECTED_KEYS[3]}
    _zip_with_csvs(src, [f"{k}_{1}.csv" for k in keep], nested=False)
    out = tmp_path / "out"
    with pytest.raises(ValueError) as exc:
        extract_archive(src, out)
    msg = str(exc.value)
    assert "avg_uptime_by_any_switch" in msg
    assert "uptime_by_device" in msg


def test_no_csvs_at_all_raises(tmp_path: Path) -> None:
    src = tmp_path / "empty.zip"
    _zip_with_csvs(src, ["readme.txt"], nested=False)
    out = tmp_path / "out"
    with pytest.raises(ValueError):
        extract_archive(src, out)


def test_nonexistent_zip_raises(tmp_path: Path) -> None:
    with pytest.raises((FileNotFoundError, OSError, ValueError)):
        extract_archive(tmp_path / "does-not-exist.zip", tmp_path / "out")


def test_required_prefixes_module_constant() -> None:
    assert set(REQUIRED_CSV_PREFIXES) == set(EXPECTED_KEYS)
    assert len(REQUIRED_CSV_PREFIXES) == 9
