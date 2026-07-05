"""Arena session/symbol/condition analytics."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.arena_analytics import (
    build_insights,
    default_analytics,
    extract_conditions,
    update_analytics,
)
from core.strategy_arena import record_outcomes, reset_arena


def _config():
    from core.utils import load_config
    return load_config()


def test_extract_conditions_from_trade_meta():
    trade = {
        "symbol": "XAUUSDm",
        "setup_type": "pullback",
        "side": "BUY",
        "signal_id": "sig-1",
        "signal_meta": {
            "market_context": {
                "session": "overlap_london_ny",
                "regime": "trending",
                "move_type": "pullback",
                "market_regime": {"primary": "strong_trend"},
            },
            "trigger_summary": "pullback retest",
            "evidence": {"trend": 0.8, "structure": 0.7},
        },
    }
    cond = extract_conditions(trade, {"sig-1": {"session": "overlap_london_ny", "regime": "strong_trend"}})
    assert cond["session"] == "overlap_london_ny"
    assert cond["regime"] == "strong_trend"
    assert cond["move_type"] == "pullback"
    assert cond["trigger_summary"] == "pullback retest"


def test_update_analytics_builds_session_and_symbol_insights():
    analytics = default_analytics()
    trigger_log = [{
        "signal_id": "s1",
        "symbol": "US500m",
        "setup_type": "trend_continuation",
        "session": "new_york",
        "regime": "strong_trend",
        "trigger_summary": "trend cont",
    }]
    win = {
        "trade_id": "w1",
        "signal_id": "s1",
        "symbol": "US500m",
        "setup_type": "trend_continuation",
        "entry": 5900.0,
        "exit": 5910.0,
        "sl": 5895.0,
        "pnl": 25.0,
        "result": "win",
        "signal_meta": {
            "market_context": {
                "session": "new_york",
                "move_type": "continuation",
                "market_regime": {"primary": "strong_trend"},
            },
        },
    }
    update_analytics(analytics, win, points=15.0, r_mult=2.0, trigger_log=trigger_log)
    insights = build_insights(analytics)
    assert insights["best_setup_per_symbol"][0]["symbol"] == "US500m"
    assert insights["best_setup_per_symbol"][0]["setup_type"] == "trend_continuation"
    assert insights["best_setup_per_session"][0]["session"] == "new_york"
    assert insights["top_winning_conditions"][0]["symbol"] == "US500m"


def test_record_outcomes_populates_insights():
    cfg = _config()
    reset_arena(cfg, campaign_id="analytics-test")
    record_outcomes(
        [{
            "trade_id": "at1",
            "symbol": "NAS100m",
            "setup_type": "breakout",
            "side": "BUY",
            "entry": 100.0,
            "exit": 102.0,
            "sl": 99.0,
            "pnl": 12.0,
            "result": "win",
            "signal_meta": {
                "market_context": {
                    "session": "london_open",
                    "market_regime": {"primary": "expansion"},
                    "move_type": "continuation",
                },
                "trigger_summary": "breakout above resistance",
            },
        }],
        cfg,
    )
    from core.strategy_arena import load_arena
    state = load_arena()
    assert state.get("insights")
    assert state["insights"].get("best_setup_per_symbol")
    assert state["analytics"]["by_symbol_setup"]["NAS100m"]["breakout"]["wins"] == 1