from __future__ import annotations

from pathlib import Path

import yaml

from core.profile_guard import assert_profile

ROOT = Path(__file__).resolve().parent.parent


def test_phase1_demo_execute_profile_is_bounded_and_demo_only():
    cfg = yaml.safe_load((ROOT / "profiles" / "phase1-demo-execute.yaml").read_text(encoding="utf-8"))
    assert_profile(cfg)

    assert cfg["mode"] == "practice"
    assert cfg["mt5"]["account_mode"] == "demo"
    assert cfg["mt5"]["symbols"] == ["XAUUSDm"]

    execution = cfg["execution"]
    assert execution["mode"] == "mt5"
    assert execution["live_trading_enabled"] is True
    assert execution["mt5_trading_enabled"] is True
    assert execution["explicit_opt_in_danger_zone"] is True
    assert execution["allow_live_account"] is False
    assert float(execution["default_lot"]) == 0.01
    assert float(execution["max_lot"]) == 0.01

    assert cfg["fast_mode"]["live_enabled"] is False
    assert cfg["trading"]["allow_pyramiding"] is False
    assert int(cfg["trading"]["max_open_per_symbol"]) == 1
    assert int(cfg["trading"]["max_session_trades_per_symbol"]) == 1
    assert cfg["trading"]["regime_flip_replace_enabled"] is False
    assert cfg["learning"]["mode"] == "observe_only"
    assert cfg["adaptation"]["enabled"] is False
    assert cfg["trade_manager"]["enabled"] is False
    assert cfg["thesis_reviewer"]["live_close_enabled"] is False
