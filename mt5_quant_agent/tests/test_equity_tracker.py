"""Tests for equity curve range slicing."""

from datetime import datetime, timedelta, timezone

from core.equity_tracker import EQUITY_RANGE_SECONDS, filter_curve_by_range


def _ts(offset_seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)).isoformat()


def test_filter_curve_by_range_1h():
    points = [
        {"ts": _ts(7200), "equity": 1000.0, "cash": 1000.0},
        {"ts": _ts(1800), "equity": 1010.0, "cash": 1010.0},
        {"ts": _ts(60), "equity": 1005.0, "cash": 1005.0},
    ]
    markers = [{"ts": _ts(1800), "equity": 1010.0, "pnl": 10.0, "result": "win"}]
    result = filter_curve_by_range(points, markers, "1h", 1000.0)

    assert result["key"] == "1h"
    assert result["point_count"] == 2
    assert len(result["markers"]) == 1
    stats = result["range_stats"]
    assert stats["period_start_equity"] == 1010.0
    assert stats["period_end_equity"] == 1005.0
    assert stats["period_change"] == -5.0


def test_filter_curve_by_range_all():
    points = [{"ts": _ts(i * 60), "equity": 1000.0 + i, "cash": 1000.0} for i in range(5)]
    result = filter_curve_by_range(points, [], "all", 1000.0)
    assert result["point_count"] == 5
    assert result["range_stats"]["period_high"] == 1004.0


def test_equity_range_keys():
    assert set(EQUITY_RANGE_SECONDS) == {"1m", "1h", "1d", "7d", "30d", "all"}


def test_range_stats_max_drawdown_from_period_start():
    points = [
        {"ts": "2026-06-25T10:00:00+00:00", "equity": 1000.0, "cash": 1000.0},
        {"ts": "2026-06-25T11:00:00+00:00", "equity": 1050.0, "cash": 1050.0},
        {"ts": "2026-06-25T12:00:00+00:00", "equity": 900.0, "cash": 900.0},
    ]
    result = filter_curve_by_range(points, [], "all", 1000.0)
    assert result["range_stats"]["period_max_drawdown_pct"] == 14.29