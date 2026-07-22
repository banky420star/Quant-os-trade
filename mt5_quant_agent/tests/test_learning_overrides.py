"""Tests for learning config overrides read-back + rollback (Phase 2.4 fix)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import learning_overrides as lo


def _cfg():
    return {
        "learning": {"mode": "live_apply_limited"},
        "signals": {"min_confidence": 50},
        "trading": {"sl_tp": {"tp1_rr": 1.5, "sl_atr_mult": 0.5}},
        "filters": {"spread_mult": 3.5},
        "fast_mode": {"max_trades_per_symbol_per_hour": 4},
    }


def _patch_store(monkeypatch, patches, rollbacks=None):
    state = {"patches": list(patches), "rollbacks": list(rollbacks or [])}

    def fake_read(name, default=None):
        if name == lo.OVERRIDES_FILE:
            return state
        return default if default is not None else {}

    def fake_write(name, doc):
        if name == lo.OVERRIDES_FILE:
            snap = dict(doc)
            state.clear()
            state.update(snap)

    monkeypatch.setattr(lo, "read_json_state", fake_read)
    monkeypatch.setattr(lo, "write_json_state", fake_write)
    return state


def test_apply_overrides_applies_safe_patch(monkeypatch):
    patches = [{"proposal_id": "p1", "patch": {"ops": [{"path": ["signals", "min_confidence"], "value": 55}]},
                "evidence": {"reviewed_trades": 40}}]
    _patch_store(monkeypatch, patches)
    merged, applied = lo.apply_learning_overrides(_cfg())
    assert merged["signals"]["min_confidence"] == 55
    assert applied == ["p1"]


def test_apply_overrides_drops_dangerous_patch_at_readback(monkeypatch):
    # lot size increase is forbidden -> dropped + rolled back at read-back
    patches = [{"proposal_id": "pbad", "patch": {"ops": [{"path": ["execution", "default_lot"], "value": 0.1}]},
                "evidence": {"reviewed_trades": 0}}]
    state = _patch_store(monkeypatch, patches)
    merged, applied = lo.apply_learning_overrides(_cfg())
    assert applied == []
    assert all(p.get("proposal_id") != "pbad" for p in state["patches"])
    assert any(r.get("proposal_id") == "pbad" for r in state.get("rollbacks", []))


def test_apply_overrides_noop_when_empty(monkeypatch):
    _patch_store(monkeypatch, [])
    merged, applied = lo.apply_learning_overrides(_cfg())
    assert applied == []
    assert merged["signals"]["min_confidence"] == 50


def test_rollback_patch_removes_entry(monkeypatch):
    patches = [{"proposal_id": "px", "patch": {"ops": []}}]
    state = _patch_store(monkeypatch, patches)
    lo.rollback_patch("px", "expectancy_degraded")
    assert state["patches"] == []
    assert state["rollbacks"][0]["proposal_id"] == "px"


def test_record_patch_baseline_stamps(monkeypatch):
    patches = [{"proposal_id": "pb", "patch": {"ops": []}}]
    state = _patch_store(monkeypatch, patches)
    lo.record_patch_baseline("pb", 0.12)
    assert state["patches"][0]["baseline_expectancy_r"] == 0.12
