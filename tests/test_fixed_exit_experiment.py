"""Regression tests for data-lab's fresh fixed-exit experiment."""

from __future__ import annotations

from copy import deepcopy


def _config() -> dict:
    from core.utils import load_config

    return deepcopy(load_config())


def test_data_lab_forces_market_and_fixed_management():
    from core.evaluation_policy import evaluate_candidate
    from core.strategy_entry import resolve_entry_mode

    cfg = _config()
    cfg["execution"].update({
        "strategy_entries_market_only": True,
        "fixed_exit_only": True,
    })
    cfg["trading"]["strategy_entries_market_only"] = True
    signal = {
        "signal_id": "fixed-1",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "entry": 99.0,
        "sl": 98.0,
        "tp1": 101.0,
        "tp2": 102.0,
        "setup_type": "pullback",
        "confidence": 80,
        "within_reach": True,
        "distance_atr": 1.5,
        "market_context": {"session": "london_open", "market_regime": {"primary": "strong_trend"}},
    }
    assert resolve_entry_mode(99.0, 100.0, 1.0, cfg) == "market"
    out = evaluate_candidate(signal, {"price": 100.0, "atr": 1.0, "volume_ratio": 1.2}, cfg)
    assert out["evaluation"]["entry_mode"] == "market"
    assert out["execution_policy"]["entry_type"] == "market"
    assert out["execution_policy"]["fixed_exit_only"] is True
    assert out["management_profile"]["break_even_enabled"] is False
    assert out["management_profile"]["trailing_enabled"] is False
    assert out["management_profile"]["partial_tp_enabled"] is False
    assert out["management_profile"]["experiment_exit_policy"] == "fixed_initial_sl_tp"


def test_full_kelly_opt_in_is_not_global_default():
    from core.kelly_sizing import clamp_risk_percent, resolve_risk_percent

    cfg = {"risk": {"max_risk_per_trade_pct": 100.0}, "signals": {"kelly_sizing": {"enabled": False}}}
    assert clamp_risk_percent(20.0, cfg) == 5.0
    cfg["risk"]["allow_full_kelly"] = True
    assert clamp_risk_percent(20.0, cfg) == 20.0
    signal = {"symbol": "XAUUSDm", "side": "BUY"}
    risk, _ = resolve_risk_percent(signal, cfg, 20.0)
    assert risk == 20.0


def test_data_lab_disables_fast_mode_and_uses_one_position_per_symbol(monkeypatch):
    monkeypatch.setenv("MT5_QUANT_PROFILE", "data-lab")
    cfg = _config()
    from core.fast_mode import fast_mode_enabled
    assert fast_mode_enabled(cfg) is False
    assert cfg["trading"]["allow_pyramiding"] is False


def test_partial_tp_helpers_leave_fixed_exit_positions_untouched():
    from core.position_manager import manage_partial_tp_paper

    cfg = _config()
    cfg["execution"]["fixed_exit_only"] = True
    positions = [{"position_id": "p1", "symbol": "XAUUSDm", "side": "BUY", "entry": 100.0, "sl": 99.0, "tp1": 101.0, "size": 1.0}]
    kept, trades, summary = manage_partial_tp_paper(cfg, positions, {"symbols": {"XAUUSDm": {"price": 100.5}}})
    assert kept == positions
    assert trades == []
    assert summary["full_closes"] == 0
