from __future__ import annotations

from pathlib import Path

import yaml

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


def test_phase1_demo_execute_uses_full_base_symbol_universe():
    base = _yaml("config.yaml")
    profile = _yaml("profiles/phase1-demo-execute.yaml")

    assert profile["mt5"]["symbols"] == base["mt5"]["symbols"]
    assert profile["practice"]["symbols"] == base["mt5"]["symbols"]
    assert len(profile["mt5"]["symbols"]) > 1


def test_phase1_demo_execute_allows_multiple_concurrent_trades():
    profile = _yaml("profiles/phase1-demo-execute.yaml")
    trading = profile["trading"]

    assert trading["aggressive_mode"] is True
    assert trading["allow_pyramiding"] is True
    assert trading["max_open_per_symbol"] > 1
    assert trading["max_session_trades_per_symbol"] > 1
    assert trading["dynamic_entries"]["enabled"] is True


def test_phase1_demo_execute_removes_canary_lot_clamp_but_keeps_broker_sizing_path():
    profile = _yaml("profiles/phase1-demo-execute.yaml")
    execution = profile["execution"]

    assert execution["default_lot"] == 0.01
    assert execution["max_lot"] > 0.01


def test_phase1_demo_execute_restores_position_management():
    profile = _yaml("profiles/phase1-demo-execute.yaml")
    trading = profile["trading"]

    assert trading["adaptive_exit"]["enabled"] is True
    assert trading["break_even"]["enabled"] is True
    assert trading["trailing"]["enabled"] is True


def test_phase1_demo_execute_relaxes_demo_exposure_caps_only():
    profile = _yaml("profiles/phase1-demo-execute.yaml")
    practice = profile["practice"]

    assert practice["max_symbol_exposure_usd"] >= 1_000_000
    assert practice["max_total_exposure_usd"] >= 10_000_000
    assert practice["max_drawdown_pct"] == 100
    assert profile["mt5"]["account_mode"] == "demo"
    assert profile["execution"]["allow_live_account"] is False
