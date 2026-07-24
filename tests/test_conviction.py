"""Tests for the conviction (professional setup-grading) engine."""

from __future__ import annotations

from core.conviction import (
    grade_signal,
    reward_risk_ratio,
    stronger_or_equal,
)


def _apex_signal():
    return {
        "symbol": "XAUUSDm", "side": "SELL", "setup_type": "trend_continuation",
        "confidence": 78, "entry": 2650.0, "sl": 2657.5, "tp1": 2635.0,  # RR 2.0
        "market_context": {"market_regime": {"primary": "strong_trend"}},
        "trade_score": {"session_detail": {"normalized": 88}},
    }


def _apex_feat():
    return {"m5_trend": "bearish", "m15_trend": "bearish", "volume_ratio": 1.8,
            "rejection": "bearish_rejection", "breakout": "none"}


def _weak_signal():
    return {
        "symbol": "EURUSDm", "side": "BUY", "setup_type": "range_fade",
        "confidence": 40, "entry": 1.0800, "sl": 1.0784, "tp1": 1.0812,  # RR ~0.75
        "market_context": {"market_regime": {"primary": "strong_trend"}},
        "trade_score": {"session_detail": {"normalized": 45}},
    }


def _weak_feat():
    return {"m5_trend": "bullish", "m15_trend": "bearish", "volume_ratio": 0.5,
            "rejection": "none", "breakout": "none"}


def test_reward_risk_ratio():
    assert reward_risk_ratio({"entry": 100, "sl": 95, "tp1": 110}) == 2.0
    assert reward_risk_ratio({"entry": 100, "sl": 95}) is None  # no tp
    assert reward_risk_ratio({"entry": 100, "sl": 100, "tp1": 110}) is None  # zero risk


def test_high_confluence_grades_A():
    r = grade_signal(_apex_signal(), _apex_feat(), {}, {"total": 40, "win_rate_pct": 62})
    assert r["grade"] in ("A", "A+")
    assert r["conviction"] >= 70
    assert r["size_mult"] == 1.0
    assert r["take"] is True
    assert "reward:risk" in r["thesis"]


def test_low_confluence_grades_C_and_downsizes():
    r = grade_signal(_weak_signal(), _weak_feat(), {}, {"total": 30, "win_rate_pct": 28})
    assert r["grade"] == "C"
    assert r["conviction"] < 56
    assert r["size_mult"] < 1.0  # weak setup is risk-trimmed


def test_min_grade_gate_skips_C():
    cfg = {"conviction": {"min_grade_to_trade": "B"}}
    r = grade_signal(_weak_signal(), _weak_feat(), cfg, {"total": 30, "win_rate_pct": 28})
    assert r["grade"] == "C"
    assert r["take"] is False  # would be skipped by the pipeline


def test_min_grade_C_takes_everything():
    cfg = {"conviction": {"min_grade_to_trade": "C"}}
    r = grade_signal(_weak_signal(), _weak_feat(), cfg, {"total": 30, "win_rate_pct": 28})
    assert r["take"] is True  # trades, but at reduced size


def test_thin_sample_history_is_neutral_not_penalised():
    # 3 trades at 20% win rate must NOT drag the grade — too little evidence.
    r_thin = grade_signal(_apex_signal(), _apex_feat(), {}, {"total": 3, "win_rate_pct": 20})
    r_none = grade_signal(_apex_signal(), _apex_feat(), {}, {})
    assert abs(r_thin["factors"]["historical_edge"] - 50.0) < 1e-6
    assert abs(r_none["factors"]["historical_edge"] - 50.0) < 1e-6


def test_size_by_grade_override():
    cfg = {"conviction": {"size_by_grade": {"A+": 1.5, "A": 1.2}}}
    r = grade_signal(_apex_signal(), _apex_feat(), cfg, {"total": 40, "win_rate_pct": 62})
    if r["grade"] == "A+":
        assert r["size_mult"] == 1.5
    elif r["grade"] == "A":
        assert r["size_mult"] == 1.2


def test_stronger_or_equal_ordering():
    assert stronger_or_equal("A+", "B") is True
    assert stronger_or_equal("A", "A") is True
    assert stronger_or_equal("C", "B") is False


def test_grade_is_deterministic():
    a = grade_signal(_apex_signal(), _apex_feat(), {}, {"total": 40, "win_rate_pct": 62})
    b = grade_signal(_apex_signal(), _apex_feat(), {}, {"total": 40, "win_rate_pct": 62})
    assert a == b


def test_conviction_sizing_scales_risk():
    """The size multiplier reaches the risk sizer via resolve_risk_percent."""
    from core.kelly_sizing import resolve_risk_percent
    cfg = {"signals": {"default_risk_percent": 1.0, "kelly_sizing": {"enabled": False}}}
    sig = {"symbol": "XAUUSDm", "side": "SELL", "setup_type": "trend_continuation",
           "conviction_size_mult": 0.6}
    frac, meta = resolve_risk_percent(sig, cfg, 1.0)
    assert abs(frac - 0.6) < 1e-6
    assert meta.get("conviction_size_mult") == 0.6
