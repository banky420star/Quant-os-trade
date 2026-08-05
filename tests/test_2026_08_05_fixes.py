"""2026-08-05 fixes from the 15-minute trade watch.

1. trail_max_r — cap trailing offset at N R-multiples of risk so an armed
   trail locks profit ABOVE entry instead of sitting below entry (which the
   capital-protection clamp then pinned to BE, giving winners back).
2. Single-producer fast path — fast_tick_loop must NOT push an open intent
   (that queued a second order drained by execution_loop ~35s later).
3. already_executed / exposure_backoff — evaluate_entry must return
   wait/already_executed instead of re-emitting FAST LIVE entry every tick,
   and execute_fast_entry stamps exposure_backoff on exposure refusals.
"""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.fast_entry_executor import evaluate_entry
from core.fast_live_executor import execute_fast_entry
from core.position_manager import compute_managed_sl


def _mgmt_cfg(trail_enabled=True, trail_max_r=None, trail_points=None):
    """Build a minimal trading cfg with USD-driven BE and trailing."""
    per_symbol_trail = {
        "XAUUSDm": {"activation_atr_mult": 0.1, "trail_points_atr_mult": 0.9},
    }
    if trail_points is not None:
        per_symbol_trail["XAUUSDm"]["trail_points"] = trail_points
    if trail_max_r is not None:
        per_symbol_trail["XAUUSDm"]["trail_max_r"] = trail_max_r
    return {
        "trading": {
            "exits": {
                "min_r_multiple_win": 0.4,
                "partial_tp": {"enabled": False},
                "runner": {"lock_profit_rr": 0.45, "trail_tighten_mult": 0.7},
                "defer_trail_until": {"require_partial_or_rr": False, "min_rr": 0.0},
            },
            "break_even": {
                "enabled": False,
                "trigger_profit_usd": 9999,
                "lock_profit_usd": 0,
                "per_symbol": {},
            },
            "trailing": {
                "enabled": trail_enabled,
                "activation_profit_usd": None,
                "activation_atr_mult": 0.1,
                "trail_points": 350,
                "trail_max_r": trail_max_r if trail_max_r is not None else 0.6,
                "per_symbol": per_symbol_trail,
            },
            "max_open_per_symbol": 1,
        },
        "trade_manager": {"enabled": True, "distance_first_triggers": False},
    }


def _pos(side="BUY", entry=1.0800, sl=1.0780):
    return {"symbol": "EURUSDm", "side": side, "entry": entry, "sl": sl}


# ---------------------------------------------------------------------------
# 1. trail_max_r cap
# ---------------------------------------------------------------------------

def test_trail_cap_keeps_stop_above_entry_at_modest_mfe():
    """With trail_points 350 (35 pips) and a 20-pip SL, an armed trail at
    +0.67R would previously sit below entry and get clamped to BE. trail_max_r
    0.6 must cap the offset so the stop locks a small profit instead."""
    cfg = _mgmt_cfg(trail_max_r=0.6, trail_points=350)
    # entry 1.0800, SL 1.0780 -> risk 0.0020 (20 pips); price +13 pips = 0.67R
    pos = _pos(entry=1.0800, sl=1.0780)
    new_sl, row, actions = compute_managed_sl(
        cfg, pos, current_price=1.0813, atr=0.001, mgmt_row={}, point=1e-5,
    )
    assert "trail" in actions
    # uncapped trail = 350 * 1e-5 = 0.0035 -> trail_sl = 1.0813-0.0035 = 1.0778 < entry
    # capped trail = 0.6 * 0.002 = 0.0012 -> trail_sl = 1.0801 > entry
    assert new_sl > 1.0800, f"trail must lock above entry, got {new_sl}"
    assert new_sl == pytest.approx(1.0813 - 0.0012, abs=1e-9)


def test_trail_cap_respects_per_symbol_override():
    """Per-symbol trail_max_r override wins over the base value."""
    cfg = _mgmt_cfg(trail_max_r=0.6, trail_points=350)
    cfg["trading"]["trailing"]["per_symbol"]["EURUSDm"] = {"trail_max_r": 0.3}
    pos = _pos(entry=1.0800, sl=1.0780)
    new_sl, row, actions = compute_managed_sl(
        cfg, pos, current_price=1.0820, atr=0.001, mgmt_row={}, point=1e-5,
    )
    assert "trail" in actions
    # cap = 0.3 * 0.002 = 0.0006 -> trail_sl = 1.0820 - 0.0006 = 1.0814
    assert new_sl == pytest.approx(1.0820 - 0.0006, abs=1e-9)


def test_trail_no_cap_when_max_r_zero():
    """trail_max_r=0 disables the cap (legacy behavior preserved)."""
    cfg = _mgmt_cfg(trail_max_r=0, trail_points=350)
    pos = _pos(entry=1.0800, sl=1.0780)
    new_sl, row, actions = compute_managed_sl(
        cfg, pos, current_price=1.0820, atr=0.001, mgmt_row={}, point=1e-5,
    )
    assert "trail" in actions
    # uncapped: 1.0820 - 0.0035 = 1.0785 (below entry, then BE-clamped to 1.0800)
    assert new_sl == pytest.approx(1.0800, abs=1e-9)


# ---------------------------------------------------------------------------
# 2. already_executed dedupe in evaluate_entry
# ---------------------------------------------------------------------------

def _fast_config(monkeypatch, **overrides):
    from core.utils import load_config
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg["execution"]["mode"] = "mt5"
    cfg["execution"]["live_trading_enabled"] = True
    cfg["execution"]["fast_mode_enabled"] = True  # data-lab gate must be on
    cfg["fast_mode"]["live_enabled"] = True
    cfg["fast_mode"]["require_verifier_approval"] = False
    cfg["fast_mode"]["min_tick_momentum_score"] = 0
    cfg["fast_mode"]["allow_market_entries"] = True
    for k, v in overrides.items():
        cfg["fast_mode"][k] = v
    return cfg


def test_evaluate_entry_skips_already_executed(monkeypatch):
    cfg = _fast_config(monkeypatch)
    monkeypatch.setattr(
        "core.fast_entry_executor.read_json_state",
        lambda name, default=None: (
            {"kill_switch": False}
            if name == "kill_switch.json"
            else {"status": "healthy"}
            if name == "health.json"
            else {"positions": []}
            if name == "mt5_positions.json"
            else default
        ),
    )
    cache_entry = {
        "symbol": "XAUUSDm", "side": "BUY", "signal_id": "sig-dup",
        "anchor": 2400.0, "entry_type": "market", "trigger_zone_atr": 0.08,
    }
    feat = {"atr_14": 2.5, "spread_points": 20, "volume_ratio": 1.1}
    state = {"executed_signals": ["sig-dup"], "exposure_backoff": {},
             "trades_hour": {}, "trades_10min": {}, "consecutive_losses": {},
             "last_loss_at": {}}
    dec = evaluate_entry(
        cache_entry, mid=2400.1, spread_points=22.0, feat=feat,
        config=cfg, state=state,
    )
    assert dec["action"] == "wait"
    assert "already_executed" in dec.get("reasons", [])


def test_evaluate_entry_skips_exposure_backoff(monkeypatch):
    cfg = _fast_config(monkeypatch, exposure_backoff_seconds=60)
    monkeypatch.setattr(
        "core.fast_entry_executor.read_json_state",
        lambda name, default=None: (
            {"kill_switch": False}
            if name == "kill_switch.json"
            else {"status": "healthy"}
            if name == "health.json"
            else {"positions": []}
            if name == "mt5_positions.json"
            else default
        ),
    )
    cache_entry = {
        "symbol": "XAUUSDm", "side": "BUY", "signal_id": "sig-bo",
        "anchor": 2400.0, "entry_type": "market", "trigger_zone_atr": 0.08,
    }
    feat = {"atr_14": 2.5, "spread_points": 20, "volume_ratio": 1.1}
    state = {"executed_signals": [], "exposure_backoff": {
        "sig-bo": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()},
        "trades_hour": {}, "trades_10min": {}, "consecutive_losses": {},
        "last_loss_at": {}}
    dec = evaluate_entry(
        cache_entry, mid=2400.1, spread_points=22.0, feat=feat,
        config=cfg, state=state,
    )
    assert dec["action"] == "wait"
    assert "exposure_backoff" in dec.get("reasons", [])


def test_evaluate_entry_allows_fresh_signal(monkeypatch):
    cfg = _fast_config(monkeypatch)
    monkeypatch.setattr(
        "core.fast_entry_executor.read_json_state",
        lambda name, default=None: (
            {"kill_switch": False}
            if name == "kill_switch.json"
            else {"status": "healthy"}
            if name == "health.json"
            else {"positions": []}
            if name == "mt5_positions.json"
            else default
        ),
    )
    cache_entry = {
        "symbol": "XAUUSDm", "side": "BUY", "signal_id": "sig-fresh",
        "anchor": 2400.0, "entry_type": "market", "trigger_zone_atr": 0.08,
    }
    feat = {"atr_14": 2.5, "spread_points": 20, "volume_ratio": 1.1}
    state = {"executed_signals": [], "exposure_backoff": {},
             "trades_hour": {}, "trades_10min": {}, "consecutive_losses": {},
             "last_loss_at": {}}
    dec = evaluate_entry(
        cache_entry, mid=2400.1, spread_points=22.0, feat=feat,
        config=cfg, state=state,
    )
    assert dec["action"] == "enter_market" or dec["action"].startswith("enter")


# ---------------------------------------------------------------------------
# 3. exposure_backoff stamping in execute_fast_entry
# ---------------------------------------------------------------------------

def test_execute_fast_entry_stamps_exposure_backoff(monkeypatch):
    from core import fast_live_executor as fle
    cfg = _fast_config(monkeypatch)
    written = {}

    def _fake_write_state(state):
        written["state"] = copy.deepcopy(state)

    monkeypatch.setattr(fle, "write_fast_state", _fake_write_state)
    monkeypatch.setattr(fle, "read_json_state", lambda name, default=None: (
        {"kill_switch": False}
        if name == "kill_switch.json"
        else {"orders": []}
        if name == "mt5_orders.json"
        else {"positions": []}
        if name == "mt5_positions.json"
        else {"trades": []}
        if name == "mt5_trades.json"
        else {"enabled": False}
        if name == "blue_guardian.json"
        else default
    ))
    monkeypatch.setattr(fle, "read_approved_signals", lambda _cfg: {
        "approved": [{"signal": {"signal_id": "sig-exp"}}]})
    monkeypatch.setattr(fle, "read_evaluated_signals", lambda _cfg: {
        "evaluated": [{
            "signal_id": "sig-exp", "symbol": "XAUUSDm", "side": "BUY",
            "entry": 2400.0, "sl": 2390.0, "tp1": 2410.0,
        }]})

    class _FakeBroker:
        def process_approved_signals(self, *a, **kw):
            return {
                "placed": [], "errors": [{"error": "exposure_limit_exceeded"}],
                "orders": [], "timestamp": "now", "balance": 100,
                "account": {}, "positions": [], "trades": [],
                "new_closed_trades": [],
            }

    class _FakeConn:
        def connect(self): return True
        def disconnect(self): return True

    monkeypatch.setattr(fle, "MT5Broker", lambda *a, **kw: _FakeBroker())
    monkeypatch.setattr(fle, "MT5ConnectionManager", lambda *a, **kw: _FakeConn())

    state = {"executed_signals": [], "exposure_backoff": {},
             "trades_hour": {}, "trades_10min": {}, "consecutive_losses": {},
             "last_loss_at": {}}
    result = execute_fast_entry(
        {"action": "enter_market", "entry_type": "market", "symbol": "XAUUSDm"},
        {"signal_id": "sig-exp", "symbol": "XAUUSDm", "anchor": 2400.0},
        config=cfg, state=state,
    )
    assert result.get("success") is False
    assert "sig-exp" in written["state"]["exposure_backoff"]
