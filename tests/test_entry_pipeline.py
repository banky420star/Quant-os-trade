"""Entry pipeline refinement tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.entry_pipeline import refine_candidates


def _signal(symbol="XAUUSDm", within_reach=True, dist=0.3):
    return {
        "signal_id": "t1",
        "symbol": symbol,
        "side": "BUY",
        "setup_type": "pullback",
        "entry": 100.0,
        "sl": 99.0,
        "tp1": 101.5,
        "confidence": 80,
        "within_reach": within_reach,
        "distance_atr": dist,
        "entry_mode": "market",
        "entry_anchor_price": 100.0,
        "market_price": 100.1,
        "confidence_tree": {"momentum_engine": 70, "structure_engine": 85},
        "market_context": {},
    }


def test_drops_unreachable_when_configured():
    config = {
        "trading": {
            "max_open_per_symbol": 1,
            "strategy_entries": {
                "reject_unreachable_entries": True,
                "skip_at_capacity_in_candidates": False,
                "refresh_each_cycle": False,
            },
        },
    }
    features = {"symbols": {"XAUUSDm": {"price": 105.0, "atr": 1.0, "m5_trend": "bullish"}}}
    ctx = {"symbols": {"XAUUSDm": {}}}
    out = refine_candidates([_signal(within_reach=False, dist=3.0)], features, ctx, config)
    assert out == []


def test_skips_symbol_at_capacity():
    config = {
        "trading": {
            "max_open_per_symbol": 1,
            "strategy_entries": {
                "reject_unreachable_entries": False,
                "skip_at_capacity_in_candidates": True,
                "refresh_each_cycle": False,
            },
        },
    }
    features = {"symbols": {"XAUUSDm": {"price": 100.0, "atr": 1.0}}}
    ctx = {"symbols": {"XAUUSDm": {}}}
    positions = [{"symbol": "XAUUSDm", "side": "BUY", "size": 0.01}]
    out = refine_candidates([_signal()], features, ctx, config, positions=positions)
    assert out == []


def test_assigns_entry_quality():
    config = {
        "trading": {
            "max_open_per_symbol": 3,
            "strategy_entries": {
                "reject_unreachable_entries": False,
                "skip_at_capacity_in_candidates": False,
                "refresh_each_cycle": False,
            },
        },
    }
    features = {"symbols": {"XAUUSDm": {"price": 100.1, "atr": 1.0, "m5_trend": "bullish"}}}
    ctx = {"symbols": {"XAUUSDm": {}}}
    out = refine_candidates([_signal()], features, ctx, config)
    assert len(out) == 1
    assert out[0]["entry_quality"] >= 60
    assert out[0]["entry_pipeline"]["status"] == "ready"