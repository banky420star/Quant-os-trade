from __future__ import annotations

from core.utils import load_config


def test_phase1_demo_execute_effective_config_stays_xau_only(monkeypatch):
    """Post-load sync helpers must not widen the execution canary symbol universe."""
    monkeypatch.setenv("MT5_QUANT_PROFILE", "phase1-demo-execute")
    cfg = load_config()

    assert cfg["active_profile"] == "phase1-demo-execute"
    assert cfg["mt5"]["symbols"] == ["XAUUSDm"]
    assert (cfg.get("practice") or {}).get("symbols") == ["XAUUSDm"]

    execution = cfg["execution"]
    assert execution["mode"] == "mt5"
    assert execution["live_trading_enabled"] is True
    assert execution["mt5_trading_enabled"] is True
    assert execution["explicit_opt_in_danger_zone"] is True
    assert execution["allow_live_account"] is False
    assert cfg["mt5"]["account_mode"] == "demo"
    assert execution["max_lot"] == 0.01
    assert cfg["trading"]["max_open_per_symbol"] == 1
    assert cfg["trading"]["max_session_trades_per_symbol"] == 1
    assert cfg["trading"]["allow_pyramiding"] is False
