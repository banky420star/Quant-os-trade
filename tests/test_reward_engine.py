"""Tests for the profitability reward engine."""

from __future__ import annotations

import math

from core.reward_engine import (
    credit_to_weights,
    engine_credit,
    expectancy,
    profit_factor,
    reward_score,
    trade_r_multiple,
    win_rate,
)
from core.weight_defaults import SUBSYSTEM_WEIGHTS


def test_trade_r_multiple_prefers_explicit():
    assert trade_r_multiple({"r_multiple": 2.5}) == 2.5


def test_trade_r_multiple_reconstructs_signed_from_rr():
    # Edge-DB style: unsigned rr magnitude + result sign.
    assert trade_r_multiple({"rr": 1.8, "result": "win"}) == 1.8
    assert trade_r_multiple({"rr": 1.8, "result": "loss"}) == -1.8


def test_trade_r_multiple_falls_back_to_sign_only():
    assert trade_r_multiple({"result": "win"}) == 1.0
    assert trade_r_multiple({"pnl": -5}) == -1.0
    assert trade_r_multiple({"result": "unknown"}) is None


def test_expectancy_and_winrate():
    rs = [1.0, -1.0, 2.0, -1.0]
    assert expectancy(rs) == 0.25
    assert win_rate(rs) == 0.5


def test_profit_factor():
    assert profit_factor([2.0, -1.0]) == 2.0
    assert profit_factor([1.0, 1.0]) == math.inf  # no losers
    assert profit_factor([]) == 0.0


def test_reward_score_rewards_smoothness_over_swings():
    # Same-ish total R, but the smooth series must score higher (risk-adjusted).
    smooth = [{"r_multiple": r} for r in [0.5, 0.4, 0.5, -0.2, 0.5] * 8]
    swingy = [{"r_multiple": r} for r in [3.0, -1, -1, -1, 3] * 8]
    s_smooth = reward_score(smooth)
    s_swingy = reward_score(swingy)
    assert s_smooth["score"] > s_swingy["score"]
    assert s_smooth["n"] == 40


def test_reward_score_empty_is_neutral():
    r = reward_score([])
    assert r["n"] == 0
    assert r["score"] == 0.0
    assert r["sufficient"] is False


def test_reward_score_net_losing_is_negative():
    losing = [{"r_multiple": r} for r in [-1.0, -1.0, 0.5, -1.0] * 10]
    assert reward_score(losing)["expectancy_r"] < 0
    assert reward_score(losing)["score"] < 0


def test_engine_credit_rewards_confidence_aligned_with_profit():
    trades = [
        {"r_multiple": 2.0, "confidence_tree": {"trend_engine": 80, "risk_engine": 50}},
        {"r_multiple": 1.5, "confidence_tree": {"trend_engine": 75, "risk_engine": 50}},
        {"r_multiple": -1.0, "confidence_tree": {"trend_engine": 30, "risk_engine": 50}},
    ]
    credit = engine_credit(trades)
    # trend_engine was confident on winners and cautious on the loser → positive.
    assert credit["trend_engine"] > 0
    # risk_engine stayed neutral (50) everywhere → ~zero credit.
    assert abs(credit["risk_engine"]) < 1e-6


def test_credit_to_weights_bounded_and_normalized():
    credit = {e: 0.0 for e in SUBSYSTEM_WEIGHTS}
    credit["trend_engine"] = 100.0
    credit["risk_engine"] = -100.0
    w = credit_to_weights(credit, max_shift=0.5)
    # 4-decimal rounding leaves the sum within a few thousandths of 1.0.
    assert abs(sum(w.values()) - 1.0) < 5e-3
    # winner up, loser down, but neither collapses to 0 or explodes.
    assert w["trend_engine"] > SUBSYSTEM_WEIGHTS["trend_engine"] / sum(SUBSYSTEM_WEIGHTS.values())
    assert all(v > 0 for v in w.values())


def test_credit_to_weights_no_signal_returns_baseline():
    credit = {e: 0.0 for e in SUBSYSTEM_WEIGHTS}
    w = credit_to_weights(credit)
    total = sum(SUBSYSTEM_WEIGHTS.values())
    for e in SUBSYSTEM_WEIGHTS:
        assert abs(w[e] - SUBSYSTEM_WEIGHTS[e] / total) < 1e-3
