"""Tests for dashboard/server._bounded_equity_curve — the cap that gives the
performance panel's 7D/30D range buttons real history without inflating the
summary poll payload."""

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.server import _bounded_equity_curve  # noqa: E402


def _curve(n_points: int, step_seconds: int = 30) -> dict:
    random.seed(7)
    start = datetime(2026, 6, 25, tzinfo=timezone.utc)
    pts = [
        {
            "ts": (start + timedelta(seconds=step_seconds * i)).isoformat(),
            "equity": 1000 + i * 0.001 + random.random(),
            "cash": 1000 + i * 0.001,
        }
        for i in range(n_points)
    ]
    markers = [
        {"ts": p["ts"], "equity": p["equity"], "event": "trade_close", "pnl": 1}
        for p in pts[-500:]
    ]
    return {
        "points": pts,
        "markers": markers,
        "current_equity": 1050.0,
        "current_balance": 1049.0,
        "ranges": {"7d": {}, "30d": {}},
    }


def test_bounded_downsampling_caps_points_and_preserves_span():
    curve = _curve(50_000)  # ~17.4 days at 30s cadence
    out = _bounded_equity_curve(curve)

    assert len(out["points"]) <= 1501, len(out["points"])

    def parse(s):
        return datetime.fromisoformat(s)

    span_days = (parse(out["points"][-1]["ts"]) - parse(out["points"][0]["ts"])).days
    # Full span preserved (first + last point survive the downsample).
    assert span_days >= 17, span_days
    # Endpoints intact.
    assert out["points"][0]["ts"] == curve["points"][0]["ts"]
    assert out["points"][-1]["ts"] == curve["points"][-1]["ts"]


def test_bounded_timestamps_stay_monotonic():
    out = _bounded_equity_curve(_curve(50_000))
    times = [datetime.fromisoformat(p["ts"]) for p in out["points"]]
    assert times == sorted(times)


def test_bounded_caps_markers_and_drops_ranges():
    out = _bounded_equity_curve(_curve(50_000))
    assert len(out["markers"]) == 250, len(out["markers"])
    assert out["ranges"] == {}


def test_bounded_small_curve_passes_through_unchanged():
    curve = _curve(100)
    out = _bounded_equity_curve(curve)
    assert len(out["points"]) == 100
    assert out["points"] == curve["points"]
    assert out["markers"] == curve["markers"]


def test_bounded_syncs_current_values_to_last_point():
    curve = _curve(50_000)
    out = _bounded_equity_curve(curve)
    assert out["current_equity"] == out["points"][-1]["equity"]
    assert out["current_balance"] == out["points"][-1]["cash"]


def test_bounded_empty_curve():
    out = _bounded_equity_curve({"points": [], "markers": [], "ranges": {}})
    assert out["points"] == []
    assert out["markers"] == []
    assert out["ranges"] == {}
