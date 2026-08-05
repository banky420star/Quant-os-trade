"""Focused guards for the data-lab fixed-exit MT5 experiment."""

from __future__ import annotations

from copy import deepcopy


def _cfg() -> dict:
    from core.utils import load_config

    return deepcopy(load_config())


def test_fast_entry_uses_market_only_and_mt5_position_ledger(monkeypatch):
    from core import fast_entry_executor as mod

    cfg = _cfg()
    cfg["execution"]["mode"] = "mt5"
    cfg["execution"]["strategy_entries_market_only"] = True
    cfg["trading"]["strategy_entries_market_only"] = True
    seen: list[str] = []

    def fake_read(name, default=None):
        seen.append(name)
        if name == "mt5_positions.json":
            return {"positions": []}
        return default or {}

    monkeypatch.setattr(mod, "read_json_state", fake_read)
    monkeypatch.setattr(mod, "fast_mode_settings", lambda cfg: {
        "enabled": True,
        "live_enabled": False,
        "allow_market_entries": True,
        "allow_limit_entries": True,
        "market_if_distance_atr_below": 1.0,
        "market_only_if_spread_ok": True,
        "max_open_positions": 1,
        "max_spread_mult": 10.0,
        "trigger_zone_atr": 0.5,
    })
    monkeypatch.setattr(mod, "fast_mode_live", lambda cfg: False)
    out = mod.evaluate_entry(
        {"symbol": "XAUUSDm", "side": "BUY", "signal_id": "s1", "anchor": 100.0,
         "entry_type": "limit"},
        mid=100.0,
        spread_points=1.0,
        feat={"atr": 1.0, "spread_points": 1.0},
        config=cfg,
        state={},
    )
    assert out["entry_type"] == "market"
    assert out["action"] == "would_enter_market"
    assert "mt5_positions.json" in seen
    assert "paper_positions.json" not in seen


def test_fast_signal_market_preparation_ignores_stale_limit_cache():
    from core.fast_live_executor import _prepare_signal

    cfg = _cfg()
    cfg["execution"]["strategy_entries_market_only"] = True
    cfg["trading"]["strategy_entries_market_only"] = True
    out = _prepare_signal(
        {"signal_id": "s1", "entry": 99.0},
        {"entry_type": "limit", "anchor": 98.0},
        {"action": "enter_limit", "entry_type": "limit"},
        cfg,
    )
    assert out["entry_mode"] == "market"
    assert out["entry"] == 99.0


def test_fixed_exit_ignores_learned_management_profile():
    from core.position_manager import compute_managed_sl

    cfg = _cfg()
    cfg["execution"]["fixed_exit_only"] = True
    cfg["trading"]["fixed_exit_only"] = True
    cfg["trading"]["break_even"] = {"enabled": True}
    cfg["trading"]["trailing"] = {"enabled": True}
    new_sl, row, actions = compute_managed_sl(
        cfg,
        {"symbol": "XAUUSDm", "side": "BUY", "entry": 100.0, "sl": 99.0,
         "tp1": 102.0, "management_profile": {
             "break_even_trigger_r": 0.01,
             "break_even_lock_r": 0.01,
             "trailing_enabled": True,
             "trail_start_r": 0.01,
         }},
        current_price=101.5,
        atr=1.0,
        mgmt_row={},
    )
    assert new_sl is None
    assert not any(a in {"break_even", "trail", "be70_trail", "time_tighten"} for a in actions)


def test_edge_query_empty_slice_is_insufficient(monkeypatch):
    from core.edge_database import EdgeDatabase

    db = EdgeDatabase()
    monkeypatch.setattr(db, "load", lambda: {"records": []})
    out = db.query_win_rate(setup_type="missing", min_samples=0)
    assert out["total"] == 0
    assert out["insufficient"] is True
