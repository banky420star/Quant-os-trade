"""Trade manager — scenario recipes, ghost scoring, promotion."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.trade_manager import (
    GHOST_PRESETS,
    record_ghost_outcomes,
    scenario_management_recipe,
    score_trade_under_ghost,
    snapshot_indicators,
)


def test_snapshot_indicators_extracts_keys():
    snap = snapshot_indicators({"atr": 4.2, "rsi": 55.0, "volume_ratio": 1.3, "noise": "x"})
    assert snap["atr"] == pytest.approx(4.2)
    assert snap["rsi"] == pytest.approx(55.0)
    assert "noise" not in snap


def test_scenario_recipe_wider_than_legacy_tight():
    signal = {
        "symbol": "XAUUSDm",
        "setup_type": "pullback",
        "market_context": {"session": "london_open", "regime": "trending", "market_regime": {"primary": "strong_trend"}},
    }
    recipe = scenario_management_recipe(signal, {})
    assert recipe["break_even_trigger_r"] >= 0.7
    assert recipe["trail_start_r"] >= 0.9
    assert "XAUUSDm" in recipe["scenario_key"]
    assert recipe["setup"] == "pullback"


def test_wide_ghost_beats_tight_when_full_stop_after_deep_mfe():
    """When MAE eventually hits 1R after a deep MFE, wide BE locks more profit.

    Path model: mae>=1 → BE save if mfe cleared be_trig (not trail). Tight lock
    is ~0.05R; wide lock is ~0.25R — matches the live payoff fix thesis.
    """
    trade = {
        "trade_id": "t1",
        "symbol": "XAUUSDm",
        "entry": 2000.0,
        "sl": 1990.0,  # risk 10
        "tp1": 2030.0,  # beyond mfe so TP1 not hit first
        "mae_R": 1.0,
        "mfe_R": 1.0,
        "r_multiple": -1.0,  # live full stop (tight BE never held)
    }
    tight = score_trade_under_ghost(trade, GHOST_PRESETS["tight_control"])
    wide = score_trade_under_ghost(trade, GHOST_PRESETS["wide_points"])
    assert tight is not None and wide is not None
    assert wide > tight
    assert wide > 0  # wide BE saved a live loser


def test_ghost_promotion_requires_sample(tmp_path, monkeypatch):
    from core import trade_manager as tm

    store: dict = {}

    def _read(name, default=None):
        return store.get(name, default if default is not None else {})

    def _write(name, data):
        store[name] = data

    monkeypatch.setattr(tm, "read_json_state", _read)
    monkeypatch.setattr(tm, "write_json_state", _write)

    trades = []
    for i in range(15):
        trades.append({
            "trade_id": f"g{i}",
            "symbol": "XAUUSDm",
            "entry": 2000.0,
            "sl": 1990.0,
            "tp1": 2020.0,
            "mae_R": 0.3,
            "mfe_R": 1.4,
            "r_multiple": 0.05,
        })
    cfg = {
        "trade_manager": {
            "enabled": True,
            "ghost_enabled": True,
            "promote_enabled": True,
            "min_n_promote": 12,
            "min_delta_r": 0.05,
            "majority_frac": 0.5,
            "active_ghosts": ["wide_points", "tight_control"],
        }
    }
    out = record_ghost_outcomes(trades, cfg)
    assert out["ghosts_scored"] > 0
    assert out["promoted"], "wide_points should promote over tiny live R"
    assert out["promoted"][0]["symbol"] == "XAUUSDm"
    assert "trade_manager_promotions.json" in store
    assert "symbol_be_trail_live.json" in store
