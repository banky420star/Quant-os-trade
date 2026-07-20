"""Tests for quarter-Kelly position sizing."""

from __future__ import annotations

from core.kelly_sizing import (
    aggregate_symbol_stats,
    kelly_fraction,
    kelly_for_signal,
    resolve_risk_percent,
)


def _cfg():
    return {
        "enabled": True,
        "kelly_fraction": 0.25,
        "criteria_mode": True,
        "symbol_aggregate_fallback": True,
        "min_n": 5,
        "max_fraction": 0.5,
        "fallback_fraction": 0.5,
    }


def test_quarter_kelly_formula():
    # p=0.6, PF=2 -> f* = 0.6 * 0.5 = 0.3, quarter = 0.075 -> 7.5% capped to 0.5
    stats = {"n": 10, "win_rate_pct": 60.0, "profit_factor": 2.0, "ci95": [-0.1, 0.5]}
    res = kelly_fraction(stats, _cfg())
    assert res["gated"] is False
    assert res["f_star"] == 0.3
    assert res["f_quarter"] == 0.075
    assert res["fraction"] == 0.5
    assert res["reason"] == "quarter_kelly"


def test_criteria_mode_skips_ci95_gate():
    stats = {"n": 8, "win_rate_pct": 55.0, "profit_factor": 1.8, "ci95": [-0.05, 0.2]}
    res = kelly_fraction(stats, _cfg())
    assert res["gated"] is False
    assert res["reason"] == "quarter_kelly"


def test_strict_mode_requires_ci95():
    cfg = {**_cfg(), "criteria_mode": False}
    stats = {"n": 8, "win_rate_pct": 55.0, "profit_factor": 1.8, "ci95": [-0.05, 0.2]}
    res = kelly_fraction(stats, cfg)
    assert res["gated"] is True


def test_aggregate_symbol_stats():
    sym_cells = {
        "a": {"n": 3, "win_rate_pct": 66.0, "profit_factor": 2.0, "expectancy_r": 0.5},
        "b": {"n": 4, "win_rate_pct": 50.0, "profit_factor": 3.0, "expectancy_r": 0.3},
    }
    agg = aggregate_symbol_stats(sym_cells)
    assert agg["n"] == 7
    assert 50.0 < agg["win_rate_pct"] < 60.0
    assert agg["profit_factor"] > 2.0


def test_kelly_for_signal_disabled():
    signal = {"symbol": "XAUUSDm", "setup_type": "pullback", "side": "SELL"}
    config = {"signals": {"kelly_sizing": {"enabled": False}, "default_risk_percent": 0.35}}
    res = kelly_for_signal(signal, config, 0.35)
    assert res["fraction"] == 0.35
    assert res["reason"] == "disabled"


def test_resolve_risk_percent_returns_kelly_meta():
    signal = {
        "symbol": "XAUUSDm",
        "setup_type": "pullback",
        "side": "SELL",
        "market_context": {
            "session": "new_york",
            "market_regime": {"primary": "compression", "bias": "neutral"},
        },
    }
    config = {"signals": {"kelly_sizing": _cfg(), "default_risk_percent": 0.5}}
    pct, meta = resolve_risk_percent(signal, config)
    assert pct > 0
    assert "fraction" in meta
    assert "gated" in meta