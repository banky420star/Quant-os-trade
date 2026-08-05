"""Tests for the shadow experiment manager."""

from __future__ import annotations

from core.shadow_experiment import (
    MAX_WEIGHT_SHIFT,
    MIN_ARM_TRADES,
    generate_experiments,
    promotion_verdict,
    score_arms,
)
from core.reward_engine import reward_score


def test_generate_experiments_are_bounded():
    arms = generate_experiments(config={"signals": {"min_confidence": 40, "min_risk_reward": 1.0}})
    assert len(arms) >= 5
    # every arm has an id, hypothesis and a patch
    for a in arms:
        assert a["id"] and a["hypothesis"] and a["patch"]
    # weight arms never move a weight more than MAX_WEIGHT_SHIFT of baseline
    from core.weight_defaults import SUBSYSTEM_WEIGHTS
    for a in arms:
        if a["family"] != "weight":
            continue
        for key, val in a["patch"].items():
            engine = key.split(".", 1)[1]
            base = SUBSYSTEM_WEIGHTS[engine]
            assert val <= base * (1 + MAX_WEIGHT_SHIFT) + 1e-9


def test_promotion_requires_enough_trades():
    control = reward_score([{"r_multiple": 0.1}] * 40)
    thin_arm = reward_score([{"r_multiple": 1.0}] * 5)  # great but tiny sample
    v = promotion_verdict(control, thin_arm)
    assert v["status"] == "running"


def test_promotion_when_arm_beats_control_on_sample():
    control = reward_score([{"r_multiple": r} for r in [0.1, -0.2, 0.1, -0.1, 0.0] * 8])
    strong_arm = reward_score([{"r_multiple": r} for r in [0.6, 0.5, 0.7, 0.4, 0.6] * 6])
    assert strong_arm["n"] >= MIN_ARM_TRADES
    v = promotion_verdict(control, strong_arm)
    assert v["status"] == "promote"
    assert v["reward_margin"] > 0


def test_losing_arm_is_rejected_not_promoted():
    control = reward_score([{"r_multiple": 0.3}] * 40)
    losing_arm = reward_score([{"r_multiple": -0.3}] * 40)
    v = promotion_verdict(control, losing_arm)
    assert v["status"] == "reject"


def test_promotion_requires_comparable_control_sample():
    control = reward_score([{"r_multiple": 0.1}] * 5)
    strong_arm = reward_score([{"r_multiple": 0.8}] * 30)
    v = promotion_verdict(control, strong_arm)
    assert v["status"] == "running"
    assert "control" in v["detail"]


def test_score_arms_tags_control_and_arms():
    arms = generate_experiments()
    armid = next(a["id"] for a in arms if a["family"] == "gate")
    control = [{"r_multiple": 0.0} for _ in range(40)]
    tagged = [{"r_multiple": 0.6, "experiment_arm": armid} for _ in range(30)]
    scored = score_arms(control + tagged, arms)
    winner = next(a for a in scored if a["id"] == armid)
    assert winner["reward"]["n"] == 30
    assert winner["verdict"] == "promote"
    # arms with no tagged trades stay 'running' (honest empty state, not error)
    other = next(a for a in scored if a["id"] != armid)
    assert other["verdict"] == "running"
