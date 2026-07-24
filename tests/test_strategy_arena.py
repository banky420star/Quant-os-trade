"""Tests for strategy arena scoring and setup competition."""

from __future__ import annotations

from core.kelly_sizing import kelly_fraction
from core.setup_classifier import SetupClassifier
from core.strategy_arena import (
    award_points,
    leaderboard,
    record_outcomes,
    record_triggers,
    reset_arena,
    setup_capacity_available,
)


def _config():
    return {
        "strategy_arena": {
            "enabled": True,
            "emit_all_setups": True,
            "symbols": ["XAUUSDm", "BTCUSDm"],
            "points_win": 10,
            "points_loss": -4,
            "points_per_r": 5,
        },
        "mt5": {"symbols": ["XAUUSDm", "BTCUSDm"]},
    }


def test_full_kelly_reason():
    cfg = {
        "kelly_fraction": 1.0,
        "criteria_mode": True,
        "min_n": 3,
        "max_fraction": 15.0,
        "fallback_fraction": 2.5,
    }
    stats = {"n": 5, "win_rate_pct": 60.0, "profit_factor": 2.0, "ci95": [0.1, 0.5]}
    res = kelly_fraction(stats, cfg)
    assert res["gated"] is False
    assert res["reason"] == "full_kelly"
    assert res["f_quarter"] == res["f_star"]


def test_classify_all_returns_multiple():
    clf = SetupClassifier({"intelligence": {"regime_filter": False}}, None)
    feat = {
        "price": 100.0,
        "m5_trend": "bullish",
        "m15_trend": "bullish",
        "breakout": "breakout",
        "bb_position": 0.05,
        "rejection": "bullish_rejection",
        "atr": 1.0,
        "support": 99.0,
        "resistance": 101.0,
    }
    ctx = {
        "regime": "trending",
        "move_type": "continuation",
        "phase": "compression",
        "market_regime": {"primary": "strong_trend"},
    }
    ev = {
        "trend": 0.8,
        "momentum": 0.7,
        "structure": 0.75,
        "volume": 0.7,
        "volatility": 0.6,
        "liquidity": 0.7,
        "risk": 0.2,
    }
    all_setups = clf.classify_all(feat, ctx, ev)
    assert len(all_setups) >= 2
    types = {s["setup_type"] for s in all_setups}
    assert "trend_continuation" in types or "breakout" in types


def test_arena_points_and_leaderboard(tmp_path, monkeypatch):
    import core.strategy_arena as arena_mod
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)
    monkeypatch.setattr(arena_mod, "write_json_state", utils.write_json_state)
    monkeypatch.setattr(arena_mod, "read_json_state", utils.read_json_state)

    cfg = _config()
    reset_arena(cfg, campaign_id="test")

    record_triggers(
        [
            {
                "arena_mode": True,
                "symbol": "XAUUSDm",
                "setup_type": "pullback",
                "side": "BUY",
                "confidence": 72,
                "signal_id": "s1",
                "market_context": {"session": "london_open", "market_regime": {"primary": "strong_trend"}},
            },
            {
                "arena_mode": True,
                "symbol": "XAUUSDm",
                "setup_type": "breakout",
                "side": "BUY",
                "confidence": 68,
                "signal_id": "s2",
                "market_context": {"session": "london_open", "market_regime": {"primary": "strong_trend"}},
            },
        ],
        cfg,
    )

    record_outcomes(
        [
            {
                "trade_id": "t1",
                "symbol": "XAUUSDm",
                "setup_type": "pullback",
                "side": "BUY",
                "entry": 100.0,
                "exit": 102.0,
                "sl": 99.0,
                "pnl": 20.0,
                "result": "win",
            },
            {
                "trade_id": "t2",
                "symbol": "XAUUSDm",
                "setup_type": "breakout",
                "side": "BUY",
                "entry": 100.0,
                "exit": 99.0,
                "sl": 99.5,
                "pnl": -10.0,
                "result": "loss",
            },
        ],
        cfg,
    )

    board = leaderboard()
    assert board["global_top"]["setup_type"] == "pullback"
    assert board["by_symbol"]["XAUUSDm"]["setup_type"] == "pullback"
    assert board["totals"]["wins"] == 1
    assert board["totals"]["losses"] == 1


def test_setup_capacity_blocks_duplicate_setup():
    cfg = _config()
    positions = [
        {"symbol": "XAUUSDm", "setup_type": "pullback", "side": "BUY"},
    ]
    assert setup_capacity_available(cfg, "XAUUSDm", "pullback", positions) is False
    assert setup_capacity_available(cfg, "XAUUSDm", "breakout", positions) is True


def test_arena_symbols_have_trailing_config():
    from core.position_manager import _symbol_overrides, _trail_cfg
    from core.strategy_arena import arena_settings
    from core.utils import load_config

    config = load_config()
    trail_parent = _trail_cfg(config)
    for sym in arena_settings(config)["symbols"]:
        trail_sym = _symbol_overrides(trail_parent, sym)
        # Activation threshold must exist and be positive; the exact USD value
        # is tuning (was 3, retuned per-symbol to 15-25 on 2026-07-21).
        act_usd = trail_sym.get("activation_profit_usd") or trail_parent.get("activation_profit_usd")
        assert act_usd and act_usd > 0
        has_dist = (
            trail_sym.get("trail_points")
            or trail_sym.get("trail_points_atr_mult")
            or trail_parent.get("trail_points")
        )
        assert has_dist, f"{sym} missing trail distance config"
        has_act = (
            trail_sym.get("activation_points")
            or trail_sym.get("activation_atr_mult")
            or trail_parent.get("activation_atr_mult")
        )
        assert has_act, f"{sym} missing trail activation fallback"


def test_award_points_win_beats_loss():
    settings = {"points_win": 10, "points_loss": -4, "points_per_r": 5}
    win_pts = award_points(
        {"entry": 100, "exit": 103, "sl": 99, "side": "BUY", "pnl": 30, "result": "win"},
        settings,
    )
    loss_pts = award_points(
        {"entry": 100, "exit": 98, "sl": 99, "side": "BUY", "pnl": -20, "result": "loss"},
        settings,
    )
    assert win_pts > 0
    assert loss_pts < 0
    assert win_pts > abs(loss_pts)