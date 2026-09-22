"""Unit tests for scripts/backfill.py (date planning + data-day targeting).

No browser and no network: only the pure helpers are exercised, plus one
cross-check that `target_for_data_day` really feeds `compute_range` the day
the caller asked for.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backfill import collect_dates, target_for_data_day  # noqa: E402

from switch_report.airwave_client import compute_range  # noqa: E402


def test_collect_dates_expands_inclusive_range():
    days = collect_dates(date(2026, 10, 1), date(2026, 10, 3), [])
    assert days == [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)]


def test_collect_dates_merges_and_dedupes_extra_dates():
    days = collect_dates(
        date(2026, 10, 1), date(2026, 10, 2), [date(2026, 10, 10), date(2026, 10, 1)]
    )
    assert days == [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 10)]


def test_collect_dates_accepts_extra_dates_only():
    assert collect_dates(None, None, [date(2026, 10, 10)]) == [date(2026, 10, 10)]


def test_collect_dates_single_day_range():
    d = date(2026, 10, 10)
    assert collect_dates(d, d, []) == [d]


@pytest.mark.parametrize("start,end,extra", [
    (date(2026, 10, 1), None, []),      # --start without --end
    (None, date(2026, 10, 1), []),      # --end without --start
    (date(2026, 10, 3), date(2026, 10, 1), []),  # reversed range
    (None, None, []),                   # nothing given
])
def test_collect_dates_rejects_bad_input(start, end, extra):
    with pytest.raises(ValueError):
        collect_dates(start, end, extra)


def test_target_for_data_day_pulls_that_days_data():
    """The whole point of the script: 2026-10-10 in, 2026-10-10 data out."""
    start, end = compute_range("daily", target_for_data_day(date(2026, 10, 10)))
    assert start == datetime(2026, 10, 10, 0, 0, tzinfo=start.tzinfo)
    assert end == datetime(2026, 10, 10, 23, 59, tzinfo=end.tzinfo)


def test_target_for_data_day_crosses_month_boundary():
    start, _ = compute_range("daily", target_for_data_day(date(2026, 10, 1)))
    assert start.date() == date(2026, 10, 1)