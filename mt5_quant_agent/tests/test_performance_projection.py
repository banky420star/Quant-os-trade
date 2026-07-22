"""Tests for monthly PnL projection math and multi-symbol aggregation."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.performance_projection import (
    PROJECTION_CAPITAL_USD,
    TARGET_MONTHLY_PNL_USD,
    aggregate_symbol_projections,
    benchmark_meets_target,
    project_monthly_pnl,
    replay_window_days,
    scale_pnl_to_capital,
)
from core.replay_engine import ReplayEngine


def test_replay_window_days_m5():
    # 58 bars × step 10 × 5 min = 2900 min ≈ 2.014 days
    days = replay_window_days(58, 10)
    assert 2.0 < days < 2.1


def test_scale_pnl_to_capital_linear():
    assert scale_pnl_to_capital(100, 1000, 1_000_000) == 100_000.0
    assert scale_pnl_to_capital(0, 1000) == 0.0


def test_project_monthly_pnl_formula():
    out = project_monthly_pnl(
        1000.0,
        58,
        10,
        replay_starting_cash=1_000_000,
        target_capital=1_000_000,
        target_monthly_pnl=TARGET_MONTHLY_PNL_USD,
    )
    window = replay_window_days(58, 10)
    expected_daily = round(1000.0 / window, 2)
    expected_monthly = round(expected_daily * 30, 2)
    assert out["scaled_pnl"] == 1000.0
    assert out["daily_pnl"] == expected_daily
    assert out["projected_monthly_pnl"] == expected_monthly
    assert out["meets_target"] == (expected_monthly >= TARGET_MONTHLY_PNL_USD)


def test_aggregate_uses_single_window_bars():
    """Portfolio/shared-equity replays use one bar span for combined projection."""
    symbol_results = [
        {"symbol": "XAUUSDm", "pnl_total": 3000, "bars_replayed": 58, "trades_closed": 2, "win_rate_pct": 50},
        {"symbol": "USOILm", "pnl_total": 2000, "bars_replayed": 58, "trades_closed": 1, "win_rate_pct": 100},
        {"symbol": "BTCUSDm", "pnl_total": 500, "bars_replayed": 58, "trades_closed": 1, "win_rate_pct": 100},
    ]
    agg = aggregate_symbol_projections(
        symbol_results,
        10,
        replay_starting_cash=PROJECTION_CAPITAL_USD,
        target_capital=PROJECTION_CAPITAL_USD,
    )
    single = project_monthly_pnl(
        5500.0,
        58,
        10,
        replay_starting_cash=PROJECTION_CAPITAL_USD,
        target_capital=PROJECTION_CAPITAL_USD,
    )
    assert agg["combined_bars_replayed"] == 58
    assert agg["projected_monthly_pnl"] == single["projected_monthly_pnl"]
    assert agg["combined_pnl_total"] == 5500.0


@pytest.fixture
def replay_config():
    from core.utils import load_config

    cfg = copy.deepcopy(load_config())
    cfg["execution"]["mode"] = "paper"
    cfg["execution"]["starting_cash"] = 1_000_000
    cfg["replay"]["max_bars"] = 120
    cfg["replay"]["bars_per_step"] = 20
    cfg["trading"]["aggressive_mode"] = False
    return cfg


def test_benchmark_meets_target_requires_coverage_and_pnl():
    good = benchmark_meets_target(
        {"replay_window_days": 10.0, "projected_monthly_pnl": 60_000.0},
        trades_closed=8,
    )
    assert good["coverage_ok"] is True
    assert good["pnl_ok"] is True
    assert good["meets_target"] is True

    bad_pnl = benchmark_meets_target(
        {"replay_window_days": 10.0, "projected_monthly_pnl": 30_000.0},
        trades_closed=8,
    )
    assert bad_pnl["coverage_ok"] is True
    assert bad_pnl["pnl_ok"] is False
    assert bad_pnl["meets_target"] is False


def test_real_replay_feeds_projection(replay_config):
    engine = ReplayEngine(replay_config)
    result = engine.run(symbol="XAUUSDm")
    proj = project_monthly_pnl(
        float(result["pnl_total"]),
        int(result["bars_replayed"]),
        20,
        replay_starting_cash=1_000_000,
    )
    assert result["bars_replayed"] > 0
    assert "projected_monthly_pnl" in proj
    assert "meets_target" in proj