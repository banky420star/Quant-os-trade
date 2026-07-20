"""Tests for the bounded config-proposal engine (Phase 2.4)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config_proposal import (
    apply_patch_to_config,
    is_dangerous,
    propose_from_reviews,
)


def _reviews(symbol="XAUUSDm", mistake="tp_too_early", n=4, **over):
    out = []
    for i in range(n):
        r = {"symbol": symbol, "side": "BUY", "net_profit": 0.2, "r_multiple": 0.4,
             "rating_total": 50, "mistake_categories": [mistake],
             "post_exit_max_favorable_atr": 0.6, "setup": "pullback"}
        r.update(over)
        out.append(r)
    return out


def _cfg():
    return {
        "trading": {"sl_tp": {"tp1_rr": 1.5, "sl_atr_mult": 0.5}},
        "signals": {"min_confidence": 50},
        "filters": {"spread_mult": 3.5},
        "fast_mode": {"max_trades_per_symbol_per_hour": 4},
    }


def test_proposes_bounded_tp_increase_for_tp_too_early():
    proposals = propose_from_reviews(_reviews(mistake="tp_too_early", n=4), _cfg())
    assert proposals, "expected at least one proposal"
    p = proposals[0]
    assert p["kind"] == "tp_too_early"
    op = p["patch"]["ops"][0]
    assert op["path"] == ["trading", "sl_tp", "tp1_rr"]
    assert 0 < op["delta"] <= 0.2  # bounded
    assert p["auto_apply_allowed"] is True
    assert p["rejected"] is False


def test_proposals_are_bounded_within_ranges():
    proposals = propose_from_reviews(_reviews(mistake="sl_too_tight", n=4), _cfg())
    op = proposals[0]["patch"]["ops"][0]
    assert -0.2 <= op["delta"] <= 0.2


def test_dangerous_lot_increase_rejected():
    patch = {"ops": [{"path": ["execution", "default_lot"], "prev": 0.01, "value": 0.05}]}
    dangerous, why = is_dangerous(patch)
    assert dangerous and "forbidden" in why


def test_dangerous_emergency_exit_disable_rejected():
    patch = {"ops": [{"path": ["fast_mode", "emergency_exit", "enabled"], "prev": True, "value": False}]}
    dangerous, why = is_dangerous(patch)
    assert dangerous and "guard_disabled" in why


def test_dangerous_cap_relax_without_evidence_rejected():
    patch = {"ops": [{"path": ["fast_mode", "max_trades_per_symbol_per_hour"], "prev": 4, "value": 6}]}
    dangerous, why = is_dangerous(patch, reviewed_trades=5)
    assert dangerous and "cap_relaxed" in why


def test_cap_relax_allowed_with_enough_evidence():
    patch = {"ops": [{"path": ["fast_mode", "max_trades_per_symbol_per_hour"], "prev": 4, "value": 5}]}
    dangerous, _ = is_dangerous(patch, reviewed_trades=35)
    assert not dangerous


def test_spread_guard_loosening_rejected():
    patch = {"ops": [{"path": ["filters", "spread_mult"], "prev": 3.5, "value": 5.0}]}
    dangerous, why = is_dangerous(patch)
    assert dangerous and "spread_guard_loosened" in why


def test_apply_patch_to_config_returns_modified_copy():
    cfg = _cfg()
    patch = {"ops": [{"path": ["signals", "min_confidence"], "prev": 50, "value": 55}]}
    out = apply_patch_to_config(cfg, patch)
    assert out["signals"]["min_confidence"] == 55
    assert cfg["signals"]["min_confidence"] == 50  # original untouched


def test_min_sample_required_for_proposal():
    proposals = propose_from_reviews(_reviews(n=2), _cfg())  # below min_sample=3
    assert proposals == []
