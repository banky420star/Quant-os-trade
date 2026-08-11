from __future__ import annotations

from pathlib import Path

import yaml

from core.profile_launcher import load_profile_overlay

ROOT = Path(__file__).resolve().parent.parent


def _yaml(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8")) or {}


def test_phase1_demo_execute_is_demo_only_and_explicitly_armed():
    profile = _yaml("profiles/phase1-demo-execute.yaml")
    execution = profile["execution"]

    assert profile["mt5"]["account_mode"] == "demo"
    assert execution["allow_live_account"] is False
    assert execution["mode"] == "mt5"
    assert execution["live_trading_enabled"] is True
    assert execution["mt5_trading_enabled"] is True
    assert execution["explicit_opt_in_danger_zone"] is True


def test_phase1_demo_execute_uses_30_symbol_demo_universe():
    profile = _yaml("profiles/phase1-demo-execute.yaml")
    symbols = profile["mt5"]["symbols"]

    assert len(symbols) == 30
    assert len(set(symbols)) == 30
    assert profile["practice"]["symbols"] == symbols
    assert {"XAUUSDm", "BTCUSDm", "EURUSDm", "NAS100m", "JP225m"}.issubset(symbols)


def test_phase1_demo_execute_allows_multiple_concurrent_trades():
    profile = _yaml("profiles/phase1-demo-execute.yaml")
    trading = profile["trading"]

    assert trading["aggressive_mode"] is True
    assert trading["allow_pyramiding"] is True
    assert trading["max_open_per_symbol"] > 1
    assert trading["max_session_trades_per_symbol"] > 1
    assert trading["dynamic_entries"]["enabled"] is True


def test_phase1_demo_execute_removes_canary_lot_clamp_but_keeps_demo_wall():
    profile = _yaml("profiles/phase1-demo-execute.yaml")
    execution = profile["execution"]

    assert execution["default_lot"] == 0.01
    assert execution["max_lot"] >= 100.0
    assert execution["allow_live_account"] is False
    assert profile["mt5"]["account_mode"] == "demo"


def test_phase1_demo_execute_restores_position_management():
    profile = _yaml("profiles/phase1-demo-execute.yaml")
    trading = profile["trading"]

    assert trading["adaptive_exit"]["enabled"] is True
    assert trading["break_even"]["enabled"] is True
    assert trading["trailing"]["enabled"] is True


def test_phase1_demo_execute_locks_gold_and_btc_trail_distance():
    profile = _yaml("profiles/phase1-demo-execute.yaml")
    per_symbol = profile["trading"]["trailing"]["per_symbol"]

    assert per_symbol["XAUUSDm"]["trail_points"] == 2100
    assert per_symbol["XAUUSDm"]["activation_points"] == 1
    assert per_symbol["XAUUSDm"]["trail_max_r"] == 0
    assert per_symbol["BTCUSDm"]["trail_points"] == 8000
    assert per_symbol["BTCUSDm"]["activation_points"] == 1
    assert per_symbol["BTCUSDm"]["trail_max_r"] == 0


def test_phase1_demo_execute_has_individual_tp_and_trail_settings_for_all_symbols():
    profile = _yaml("profiles/phase1-demo-execute.yaml")
    symbols = profile["mt5"]["symbols"]
    sltp = profile["trading"]["strategy_entries"]["sl_tp"]["per_symbol"]
    trail = profile["trading"]["trailing"]["per_symbol"]

    assert set(sltp) == set(symbols)
    assert set(trail) == set(symbols)
    for symbol in symbols:
        assert sltp[symbol]["tp1_rr"] > 0
        assert sltp[symbol]["tp2_rr"] >= sltp[symbol]["tp1_rr"]
        assert trail[symbol].get("trail_points") is not None or trail[symbol].get("trail_points_atr_mult") is not None


def test_phase1_demo_runtime_contract_adds_m15_rvi_to_every_symbol():
    profile = load_profile_overlay("phase1-demo-execute")
    symbols = profile["mt5"]["symbols"]
    rvi = profile["trading"]["rvi_exit"]

    assert rvi["enabled"] is True
    assert rvi["timeframe"] == "M15"
    assert rvi["period"] == 10
    assert rvi["max_data_age_seconds"] == 1200
    assert set(rvi["per_symbol"]) == set(symbols)
    assert all(rvi["per_symbol"][symbol]["period"] == 10 for symbol in symbols)


def test_phase1_demo_execute_relaxes_demo_exposure_caps_only():
    profile = _yaml("profiles/phase1-demo-execute.yaml")
    practice = profile["practice"]

    assert practice["max_symbol_exposure_usd"] >= 1_000_000
    assert practice["max_total_exposure_usd"] >= 10_000_000
    assert practice["max_drawdown_pct"] == 100
    assert profile["mt5"]["account_mode"] == "demo"
    assert profile["execution"]["allow_live_account"] is False
