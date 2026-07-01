"""Regression tests for ReplayEngine._m15_up_to look-ahead boundary.

Pins the fix for the M15 look-ahead leak (memory
backtest-lookahead-bugs-2026-06-27). M5/M15 parquets are timestamped at bar
OPEN. An M5 decision at ``bar_time`` resolves the M5 bar's CLOSE
(``bar_time + 5min``). An M15 bar opened at ``t`` closes at ``t + 15min``; it
is only KNOWN-closed by the decision time if ``t + 15 <= bar_time + 5``, i.e.
``t <= bar_time - 10min``. Including the in-progress M15 bar would leak ~10min
of its future OHLC into the trend/alignment feature.

These tests pin the boundary exactly so a future refactor cannot silently
relax the exclusion.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.replay_engine import ReplayEngine


def _m15_frame() -> pd.DataFrame:
    """Small M15 frame with bars at 15-minute open timestamps."""
    times = [
        pd.Timestamp("2026-06-25 09:45:00"),
        pd.Timestamp("2026-06-25 10:00:00"),
        pd.Timestamp("2026-06-25 10:15:00"),
        pd.Timestamp("2026-06-25 10:30:00"),
    ]
    return pd.DataFrame(
        {
            "time": times,
            "open": [1.0, 2.0, 3.0, 4.0],
            "high": [1.5, 2.5, 3.5, 4.5],
            "low": [0.5, 1.5, 2.5, 3.5],
            "close": [1.2, 2.2, 3.2, 4.2],
            "volume": [100, 200, 300, 400],
        }
    )


def test_bar_time_inside_m15_bar_excludes_it():
    """bar_time = 10:05 falls INSIDE the 10:00-10:15 M15 bar.

    That bar's close (10:15) is 10 min in the future relative to the 10:05
    decision (which resolves the 10:00-10:05 M5 close). It must be EXCLUDED.
    Only bars with open <= 10:05 - 10min = 09:55 are returned -> the 09:45
    bar only.
    """
    m15 = _m15_frame()
    bar_time = pd.Timestamp("2026-06-25 10:05:00")

    out = ReplayEngine._m15_up_to(m15, bar_time)

    out_times = list(out["time"])
    assert pd.Timestamp("2026-06-25 10:00:00") not in out_times, (
        "in-progress M15 bar (10:00-10:15) must be excluded at bar_time=10:05"
    )
    assert pd.Timestamp("2026-06-25 09:45:00") in out_times
    # Nothing at or after 10:00.
    assert all(t < pd.Timestamp("2026-06-25 10:00:00") for t in out_times)


def test_bar_time_at_m15_close_includes_it():
    """bar_time = 10:15 exactly. The 10:00-10:15 M15 bar closes at 10:15 ==
    bar_time, so it IS known-closed at the 10:15 decision time (the 10:15-10:20
    M5 close). Boundary: t <= 10:15 - 10min = 10:05 includes the 10:00 bar.
    """
    m15 = _m15_frame()
    bar_time = pd.Timestamp("2026-06-25 10:15:00")

    out = ReplayEngine._m15_up_to(m15, bar_time)

    out_times = list(out["time"])
    assert pd.Timestamp("2026-06-25 10:00:00") in out_times, (
        "10:00-10:15 M15 bar is closed by bar_time=10:15 and must be included"
    )
    # The 10:15 bar (open) is still in-progress (closes 10:30) -> excluded.
    assert pd.Timestamp("2026-06-25 10:15:00") not in out_times
    assert pd.Timestamp("2026-06-25 09:45:00") in out_times


def test_bar_time_before_first_bar_returns_empty():
    m15 = _m15_frame()
    out = ReplayEngine._m15_up_to(m15, pd.Timestamp("2026-06-25 09:00:00"))
    assert len(out) == 0


def test_bar_time_after_all_bars_includes_all():
    m15 = _m15_frame()
    out = ReplayEngine._m15_up_to(m15, pd.Timestamp("2026-06-25 11:00:00"))
    assert len(out) == 4


def test_none_df_returns_empty():
    out = ReplayEngine._m15_up_to(None, pd.Timestamp("2026-06-25 10:15:00"))
    assert len(out) == 0


def test_exclusion_boundary_is_ten_minute_offset():
    """Pin the exact inclusion boundary for the 10:00-10:15 M15 bar.

    The M5 decision at ``bar_time`` resolves at ``bar_time + 5min`` (M5 close).
    The M15 bar opened at 10:00 closes at 10:15; it is known-closed iff
    ``10:15 <= bar_time + 5min`` i.e. ``bar_time >= 10:10``.

    So:
      * bar_time = 10:09 -> 10:00-10min = 09:55 -> 10:00 > 09:55 -> EXCLUDED
        (decision at 10:14, M15 closes at 10:15 -> still in-progress: look-ahead)
      * bar_time = 10:10 -> 10:00-10min = 10:00 -> 10:00 <= 10:00 -> INCLUDED
        (decision at 10:15, M15 closes at 10:15 -> known-closed: no leak)

    A naive ``t <= bar_time`` (no 10-minute offset) would include the 10:00
    bar already at 10:05 — a look-ahead leak. These two boundary points lock
    the 10-minute offset.
    """
    m15 = _m15_frame()

    out_09 = ReplayEngine._m15_up_to(m15, pd.Timestamp("2026-06-25 10:09:00"))
    assert pd.Timestamp("2026-06-25 10:00:00") not in list(out_09["time"]), (
        "10:00 M15 bar must be excluded at bar_time=10:09 "
        "(decision 10:14 < close 10:15 -> in-progress -> look-ahead)"
    )

    out_10 = ReplayEngine._m15_up_to(m15, pd.Timestamp("2026-06-25 10:10:00"))
    assert pd.Timestamp("2026-06-25 10:00:00") in list(out_10["time"]), (
        "10:00 M15 bar must be included at bar_time=10:10 "
        "(decision 10:15 == close 10:15 -> known-closed -> no leak)"
    )