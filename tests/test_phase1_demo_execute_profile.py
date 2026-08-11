from __future__ import annotations

from pathlib import Path

import yaml

from core.profile_guard import assert_profile

ROOT = Path(__file__).resolve().parent.parent


def test_phase1_demo_execute_profile_is_high_throughput_and_demo_only():
    cfg = yaml.safe_load((ROOT / "profiles" / "phase1-demo-execute.yaml").read_text(encoding="utf-8"))
    assert_profile(cfg)

    symbols = cfg["mt5"]["symbols"]
    assert cfg["mode"] == "practice"
    assert len(symbols) == 30
    assert len(set(symbols)) == 30
    assert cfg["practice"]["symbols"] == symbols
    assert cfg["practice"]["require_top_ranked_setup"] is False
    assert cfg["mt5"]["account_mode"] == "demo"

    execution = cfg["execution"]
    assert execution["mode"] == "mt5"
    assert execution["live_trading_enabled"] is True
    assert execution["mt5_trading_enabled"] is True
    assert execution["explicit_opt_in_danger_zone"] is True
    assert execution["allow_live_account"] is False
    assert float(execution["default_lot"]) == 0.01
    assert float(execution["max_lot"]) >= 100.0

    assert cfg["fast_mode"]["live_enabled"] is False
    assert cfg["trading"]["allow_pyramiding"] is True
    assert int(cfg["trading"]["max_open_per_symbol"]) > 1
    assert int(cfg["trading"]["max_session_trades_per_symbol"]) > 1
    assert cfg["trading"]["regime_flip_replace_enabled"] is False
    assert cfg["learning"]["mode"] == "observe_only"
    assert cfg["adaptation"]["enabled"] is False
    assert cfg["trade_manager"]["enabled"] is False
    assert cfg["thesis_reviewer"]["live_close_enabled"] is False


def test_effective_config_keeps_30_symbol_scope_and_demo_wall(monkeypatch):
    from core.utils import load_config

    monkeypatch.setenv("MT5_QUANT_PROFILE", "phase1-demo-execute")
    cfg = load_config()

    symbols = cfg["mt5"]["symbols"]
    assert cfg["active_profile"] == "phase1-demo-execute"
    assert len(symbols) == 30
    assert cfg["practice"]["symbols"] == symbols
    assert cfg["quant"]["strategy_ranking_enabled"] is True
    assert cfg["quant"]["require_top_ranked_setup"] is False
    assert cfg["execution"]["allow_live_account"] is False
    assert float(cfg["execution"]["max_lot"]) >= 100.0
