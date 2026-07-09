"""Tests for the learning review loop (Phase 2.4)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loops import learning_review_loop as L


def _trades(n=4):
    out = []
    for i in range(n):
        out.append({
            "trade_id": f"t{i}", "symbol": "XAUUSDm", "side": "BUY", "result": "win",
            "entry": 4070.0, "exit": 4077.0, "sl": 4060.0, "tp1": 4080.0,
            "pnl": 0.2, "r_multiple": 0.4, "mfe_R": 0.4, "mae_R": -0.1,
            "exit_reason": "take_profit", "session": "new_york", "regime_primary": "weak_trend",
            "hold_seconds": 120, "opened_at": "2026-07-08T19:00:00+00:00",
            "features_at_entry": {"atr": 5.0, "m5_trend": "bullish", "m15_trend": "bullish",
                                  "bb_position": 0.8, "spread_points": 20},
            "signal_meta": {"distance_atr": 0.1, "entry_mode": "market"},
        })
    return out


def _patch_loop(monkeypatch, *, mode, trades, writes=None):
    writes = writes if writes is not None else {}

    def fake_load_config():
        return {
            "learning": {"mode": mode, "proposal_min_sample": 3, "tp_early_atr_threshold": 0.3,
                         "overtrading_window_minutes": 10, "overtrading_cap": 4},
            "trading": {"sl_tp": {"tp1_rr": 1.5, "sl_atr_mult": 0.5}},
            "signals": {"min_confidence": 50},
            "filters": {"spread_mult": 3.5},
            "fast_mode": {"max_trades_per_symbol_per_hour": 4},
        }

    def fake_read(name, default=None):
        if name == "trade_log.json":
            return {"trades": trades}
        return default if default is not None else {}

    def fake_write(name, doc):
        writes[name] = doc

    monkeypatch.setattr(L, "load_config", fake_load_config)
    monkeypatch.setattr(L, "read_json_state", fake_read)
    monkeypatch.setattr(L, "write_json_state", fake_write)
    monkeypatch.setattr(L, "log_review", lambda r: None)
    monkeypatch.setattr(L, "log_config_proposal", lambda p: None)
    monkeypatch.setattr(L, "log_config_change", lambda e: None)
    monkeypatch.setattr(L, "config_snapshot_hash", lambda c: "hash123")
    return writes


def test_observe_only_does_not_review_or_modify_config(monkeypatch):
    writes = _patch_loop(monkeypatch, mode="observe_only", trades=_trades())
    state = L.run()
    assert state["mode"] == "observe_only"
    assert state["reviewed_count"] == 0  # no review happened
    assert state.get("active_proposals") == []
    # observe_only must NOT write the runtime override / config patch file
    assert "learning_config_overrides.json" not in writes


def test_review_only_scores_trades_and_updates_state(monkeypatch):
    writes = _patch_loop(monkeypatch, mode="review_only", trades=_trades(4))
    state = L.run()
    assert state["reviewed_count"] == 4
    assert state["mode"] == "review_only"
    assert "learning_state.json" in writes
    # review_only does not propose
    assert state.get("active_proposals") == []


def test_propose_only_writes_proposals(monkeypatch):
    # trades that exited early -> should generate a tp_too_early proposal
    trades = _trades(4)
    for t in trades:
        t["r_multiple"] = 0.3
        t["mfe_R"] = 1.4  # captured only 0.3R of 1.4R -> tp_too_early
    _patch_loop(monkeypatch, mode="propose_only", trades=trades)
    state = L.run()
    assert state["reviewed_count"] == 4
    assert len(state.get("active_proposals") or []) >= 1


def test_live_apply_limited_only_applies_safe_proposals(monkeypatch):
    trades = _trades(4)
    for t in trades:
        t["r_multiple"] = 0.3
        t["mfe_R"] = 1.4
    writes = _patch_loop(monkeypatch, mode="live_apply_limited", trades=trades)
    state = L.run()
    # a bounded tp_too_early proposal is low-risk + auto_apply_allowed -> applied
    overrides = writes.get("learning_config_overrides.json")
    assert overrides is not None
    assert len(state.get("applied_patches") or []) >= 1


def test_loop_does_not_place_trades():
    # The learning loop must contain no broker/execution code.
    src = (Path(L.__file__)).read_text(encoding="utf-8")
    assert "mt5_broker" not in src
    assert "process_approved" not in src
    assert "order_send" not in src


def test_unknown_mode_falls_back_to_observe_only(monkeypatch):
    _patch_loop(monkeypatch, mode="nonsense_mode", trades=_trades())
    state = L.run()
    assert state["mode"] == "observe_only"
    assert state["reviewed_count"] == 0


def _candles_doc(after_prices):
    """Build a latest_candles-like doc with M5 candles after 2026-07-08T19:30."""
    candles = []
    base = "2026-07-08T19:35:00+00:00"
    for i, px in enumerate(after_prices):
        candles.append({"time": f"2026-07-08T19:{35+i:02d}:00+00:00",
                        "open": px, "high": px, "low": px - 1, "close": px, "volume": 100})
    return {"symbols": {"XAUUSDm": {"M5": candles}}}


def test_post_exit_prices_helper():
    from loops.learning_review_loop import _post_exit_prices
    trade = {"symbol": "XAUUSDm", "side": "BUY", "closed_at": "2026-07-08T19:30:00+00:00"}
    doc = _candles_doc([4077.0, 4082.0, 4087.0])
    prices = _post_exit_prices(trade, doc)
    assert prices == [4077.0, 4082.0, 4087.0]


def test_review_uses_post_exit_candles_for_tp_too_early(monkeypatch):
    trades = [{
        "trade_id": "tx", "symbol": "XAUUSDm", "side": "BUY", "result": "win",
        "entry": 4070.0, "exit": 4077.0, "sl": 4060.0, "tp1": 4080.0,
        "pnl": 0.3, "r_multiple": 0.7, "mfe_R": 0.7, "mae_R": -0.1,
        "exit_reason": "take_profit", "session": "new_york", "regime_primary": "weak_trend",
        "closed_at": "2026-07-08T19:30:00+00:00", "hold_seconds": 120,
        "features_at_entry": {"atr": 5.0, "m5_trend": "bullish", "m15_trend": "bullish",
                              "bb_position": 0.8, "spread_points": 20},
        "signal_meta": {"distance_atr": 0.1, "entry_mode": "market"},
    }]
    doc = _candles_doc([4077.0, 4082.0, 4087.0])  # price kept rising 1.0 ATR after exit

    def fake_read(name, default=None):
        if name == "trade_log.json":
            return {"trades": trades}
        if name == "latest_candles.json":
            return doc
        return default if default is not None else {}

    writes = {}
    monkeypatch.setattr(L, "load_config", lambda: {"learning": {"mode": "review_only", "proposal_min_sample": 3}})
    monkeypatch.setattr(L, "read_json_state", fake_read)
    monkeypatch.setattr(L, "write_json_state", lambda n, d: writes.__setitem__(n, d))
    monkeypatch.setattr(L, "log_review", lambda r: None)
    state = L.run()
    # the review must have used post-exit movement -> tp_too_early detected
    reviews = state.get("recent_ratings")
    assert state["reviewed_count"] == 1
    # find the review via the logged call would need capture; check via mistake_counts instead
    assert state.get("mistake_counts", {}).get("tp_too_early", 0) >= 1


def test_check_rollbacks_drops_degraded_patch(monkeypatch):
    rolled = []
    monkeypatch.setattr(L, "read_overrides", lambda: {"patches": [
        {"proposal_id": "px", "baseline_expectancy_r": 0.2, "applied_at_reviewed": 10}], "rollbacks": []})
    monkeypatch.setattr(L, "rollback_patch", lambda pid, reason: rolled.append((pid, reason)))
    monkeypatch.setattr(L, "log_config_change", lambda e: None)
    state = {"rolling_expectancy_r": 0.0, "reviewed_count": 20, "rollback_triggers": []}
    out = L._check_rollbacks(state)
    assert rolled and rolled[0][0] == "px"
    assert any(t.get("proposal_id") == "px" for t in out["rollback_triggers"])


def test_check_rollbacks_keeps_patch_when_performance_ok(monkeypatch):
    rolled = []
    monkeypatch.setattr(L, "read_overrides", lambda: {"patches": [
        {"proposal_id": "pok", "baseline_expectancy_r": 0.05, "applied_at_reviewed": 10}], "rollbacks": []})
    monkeypatch.setattr(L, "rollback_patch", lambda pid, reason: rolled.append(pid))
    monkeypatch.setattr(L, "log_config_change", lambda e: None)
    state = {"rolling_expectancy_r": 0.2, "reviewed_count": 20, "rollback_triggers": []}
    L._check_rollbacks(state)
    assert rolled == []
