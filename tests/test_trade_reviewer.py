"""Tests for the trade review engine (Phase 2.4)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.trade_reviewer import (
    detect_missed_trade_after_skip,
    detect_overtrading,
    detect_spread_spike,
    detect_wrong_timeframe_alignment,
    review_trade,
)


def _win_trade(**over):
    base = {
        "trade_id": "t1", "symbol": "XAUUSDm", "side": "BUY", "result": "win",
        "entry": 4070.0, "exit": 4077.0, "sl": 4060.0, "tp1": 4080.0,
        "pnl": 0.30, "r_multiple": 0.7, "mfe_R": 0.7, "mae_R": -0.1,
        "exit_reason": "take_profit", "session": "new_york",
        "regime_primary": "weak_trend", "hold_seconds": 120,
        "features_at_entry": {"atr": 5.0, "m5_trend": "bullish", "m15_trend": "bullish",
                              "bb_position": 0.8, "spread_points": 20},
        "signal_meta": {"distance_atr": 0.1, "entry_mode": "market"},
    }
    base.update(over)
    return base


def test_tp_too_early_detected_via_post_exit_movement():
    t = _win_trade(r_multiple=0.7, mfe_R=0.7, exit_reason="take_profit")
    # price kept rising 1.0 ATR after exit -> left money on the table
    rev = review_trade(t, post_exit_prices=[4077.0, 4082.0, 4087.0], config={})
    assert "tp_too_early" in rev["mistake_categories"]
    assert rev["post_exit_max_favorable_atr"] >= 0.3


def test_tp_too_early_detected_via_mfe_vs_realized():
    t = _win_trade(r_multiple=0.3, mfe_R=1.4)  # captured only 0.3R of a 1.4R move
    rev = review_trade(t, config={})
    assert "tp_too_early" in rev["mistake_categories"]


def test_sl_too_tight_detected_when_price_reverses_after_stop():
    t = _win_trade(result="loss", r_multiple=-1.0, mae_R=-0.15, mfe_R=0.6,
                   exit_reason="stop_loss", pnl=-0.2)
    # after SL hit, price ran in the trade direction
    rev = review_trade(t, post_exit_prices=[4060.0, 4068.0, 4075.0], config={})
    assert "sl_too_tight" in rev["mistake_categories"]


def test_wrong_timeframe_alignment_detected():
    t = _win_trade(features_at_entry={"m5_trend": "bullish", "m15_trend": "bearish",
                                      "atr": 5.0, "spread_points": 20})
    assert detect_wrong_timeframe_alignment(t) is True
    rev = review_trade(t, config={})
    assert "wrong_timeframe_alignment" in rev["mistake_categories"]


def test_spread_spike_detected():
    assert detect_spread_spike(80, 20, max_mult=2.0) is True
    assert detect_spread_spike(25, 20, max_mult=2.0) is False
    t = _win_trade(features_at_entry={"m5_trend": "bullish", "m15_trend": "bullish",
                                      "atr": 5.0, "spread_points": 90})
    rev = review_trade(t, baseline_spread=20, config={})
    assert "spread_spike_entry" in rev["mistake_categories"]


def test_overtrading_detected():
    now = datetime.now(timezone.utc)
    trades = [{"symbol": "XAUUSDm", "opened_at": (now - timedelta(minutes=i)).isoformat()}
              for i in range(6)]
    assert detect_overtrading(trades, "XAUUSDm", cap=4) is True
    assert detect_overtrading(trades[:3], "XAUUSDm", cap=4) is False


def test_chop_zone_and_bad_session_detected():
    t = _win_trade(regime_primary="compression", session="rollover",
                   features_at_entry={"m5_trend": "bullish", "m15_trend": "bullish",
                                      "atr": 5.0, "spread_points": 20, "bb_position": 0.5})
    rev = review_trade(t, config={})
    assert "chop_zone_entry" in rev["mistake_categories"]
    assert "bad_session" in rev["mistake_categories"]


def test_missed_trade_after_skip():
    skip = {"side": "BUY", "price": 4070.0, "atr": 5.0}
    res = detect_missed_trade_after_skip(skip, [4070.0, 4078.0, 4086.0], min_move_atr=0.8)
    assert res is not None
    assert res["mistake_type"] == "missed_trade_after_skip"


def test_review_produces_scores_and_total():
    rev = review_trade(_win_trade(), config={})
    for k in ("entry_timing_score", "exit_quality_score", "risk_score",
              "trend_alignment_score", "execution_score", "rating_total"):
        assert 0 <= rev[k] <= 100
    assert rev["trade_id"] == "t1"


def test_review_handles_sparse_trade_without_crashing():
    sparse = {"trade_id": "old", "symbol": "XAUUSDm", "side": "BUY", "result": "win", "pnl": 0.1}
    rev = review_trade(sparse, config={})
    assert rev["trade_id"] == "old"
    assert isinstance(rev["mistake_categories"], list)
