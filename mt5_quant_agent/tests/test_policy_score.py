"""Policy score unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.policy_score import compute_policy_score, recent_symbol_stats, session_entry_bias


def test_recent_symbol_stats_empty():
    stats = recent_symbol_stats([], "XAUUSDm")
    assert stats["n"] == 0
    assert stats["win_rate_pct"] == 50.0


def test_recent_symbol_stats_win_rate():
    trades = [
        {"symbol": "XAUUSDm", "result": "win", "pnl": 0.5},
        {"symbol": "XAUUSDm", "result": "loss", "pnl": -0.3},
        {"symbol": "USOILm", "result": "win", "pnl": 1.0},
    ]
    stats = recent_symbol_stats(trades, "XAUUSDm")
    assert stats["n"] == 2
    assert stats["wins"] == 1
    assert stats["win_rate_pct"] == 50.0


def test_session_entry_bias_london():
    bias = session_entry_bias("london_open")
    assert bias["entry_type"] == "market"
    assert bias["session_weight"] > 0