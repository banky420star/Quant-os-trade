"""Tests for babysit observe metrics — real shipped functions."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.babysit_metrics import (
    detect_regressions,
    equity_from_account,
    observe_snapshot,
    trade_book_summary,
    xau_be_points,
)


def test_equity_prefers_equity_over_balance():
    assert equity_from_account({"equity": 96.12, "balance": 99.8}) == pytest.approx(96.12)
    assert equity_from_account({"balance": 500_000}) == pytest.approx(500_000)
    assert equity_from_account({}) is None


def test_trade_book_payoff_and_expectancy():
    trades = [
        {"symbol": "XAUUSDm", "setup": "pullback", "result": "win", "pnl": 1.0, "r_multiple": 0.5, "entry": 1, "exit": 2},
        {"symbol": "XAUUSDm", "setup": "pullback", "result": "win", "pnl": 1.0, "r_multiple": 0.5, "entry": 1, "exit": 2},
        {"symbol": "USOILm", "setup": "trend_continuation", "result": "loss", "pnl": -4.0, "r_multiple": -1.0, "entry": 1, "exit": 0},
    ]
    book = trade_book_summary(trades)
    assert book["n"] == 3
    assert book["win_rate_pct"] == pytest.approx(66.67, abs=0.1)
    assert book["total_pnl"] == pytest.approx(-2.0)
    assert book["avg_win"] == pytest.approx(1.0)
    assert book["avg_loss"] == pytest.approx(-4.0)
    assert book["payoff_ratio"] == pytest.approx(0.25)
    assert book["expectancy_r"] == pytest.approx(0.0, abs=0.01)
    assert book["by_symbol"]["USOILm"]["pnl"] == pytest.approx(-4.0)


def test_detect_tight_be_and_missing_blocks():
    config = {
        "trading": {
            "break_even": {
                "per_symbol": {
                    "XAUUSDm": {"trigger_profit_usd": 2, "trigger_points": 100},
                }
            },
            "trailing": {"enabled": True},
        },
        "evaluation": {"symbol_blocklist": [], "setup_blocklist": []},
    }
    book = {"n": 30, "win_rate_pct": 65, "expectancy_r": -0.1, "payoff_ratio": 0.4,
            "entry_eq_exit_count": 0, "r_coverage": 30}
    issues = detect_regressions(config, book, {"enabled": True, "tier": "normal", "recent_net_pnl": -20})
    ids = {i["id"] for i in issues}
    assert "tight_be_usd" in ids
    assert "missing_block_USOILm" in ids
    assert "missing_setup_block_trend" in ids
    assert "high_wr_neg_expectancy" in ids
    assert "gates_ignore_net_pnl" in ids


def test_wide_be_config_passes_xau_points():
    config = {
        "trading": {
            "break_even": {
                "per_symbol": {
                    "XAUUSDm": {
                        "trigger_profit_usd": 25,
                        "trigger_points": 5000,
                        "lock_profit_points": 3000,
                    }
                }
            },
            "trailing": {"enabled": True},
        },
        "evaluation": {
            "symbol_blocklist": ["USOILm", "UK100m"],
            "setup_blocklist": ["trend_continuation"],
        },
    }
    be = xau_be_points(config)
    assert be["trigger_points"] == 5000
    book = {"n": 10, "win_rate_pct": 50, "expectancy_r": 0.05, "payoff_ratio": 1.1,
            "entry_eq_exit_count": 0, "r_coverage": 10}
    issues = detect_regressions(config, book, {"enabled": True, "tier": "cautious", "recent_net_pnl": -3})
    assert not any(i["id"].startswith("tight_be") for i in issues)
    assert not any(i["id"].startswith("missing_block") for i in issues)


def test_observe_snapshot_equity_gate_flag():
    snap = observe_snapshot(
        account={"equity": 96.12, "balance": 99.8},
        supervisor={"overall_status": "healthy", "services": [{"name": "trading_pipeline", "status": "running"}]},
        trade_log={"trades": [
            {"symbol": "EURUSDm", "setup": "pullback", "result": "win", "pnl": 2.0, "r_multiple": 0.4, "entry": 1.1, "exit": 1.11},
        ]},
        config={
            "trading": {
                "break_even": {"per_symbol": {"XAUUSDm": {"trigger_points": 5000, "trigger_profit_usd": 25}}},
                "trailing": {"enabled": True},
            },
            "evaluation": {
                "symbol_blocklist": ["USOILm", "UK100m"],
                "setup_blocklist": ["trend_continuation"],
            },
        },
        gates={"enabled": True, "tier": "cautious", "recent_net_pnl": -9, "reason": "test"},
    )
    assert snap["equity"] == pytest.approx(96.12)
    assert snap["equity_ge_500000"] is False
    assert snap["supervisor_ok"] is True
    assert snap["book"]["n"] == 1
