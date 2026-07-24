"""Tests for the learning self-monitor."""

from __future__ import annotations

from core.learning_monitor import assess_learning_performance


def _series(rs, day):
    return [
        {"r_multiple": r, "closed_at": f"2026-{day:02d}-01T00:{i:02d}:00"}
        for i, r in enumerate(rs)
    ]


def test_degrading_recommends_rollback():
    good = _series([0.5, 0.6, 0.4, 0.5, 0.5, 0.6, 0.4, 0.5, 0.5, 0.6] * 3, 1)
    bad = _series([-0.5, -0.6, 0.2, -0.5, -0.4, -0.6, 0.1, -0.5, -0.5, -0.6] * 3, 2)
    v = assess_learning_performance(good + bad)
    assert v["verdict"] == "degrading"
    assert v["rollback_recommended"] is True
    assert v["score_delta"] < 0


def test_improving_does_not_rollback():
    bad = _series([-0.2, 0.1, -0.3, 0.0, -0.2, 0.1, -0.3, 0.0, -0.2, 0.1] * 3, 1)
    good = _series([0.6, 0.5, 0.7, 0.4, 0.6, 0.5, 0.7, 0.4, 0.6, 0.5] * 3, 2)
    v = assess_learning_performance(bad + good)
    assert v["verdict"] == "improving"
    assert v["rollback_recommended"] is False


def test_insufficient_sample_never_rolls_back():
    v = assess_learning_performance([{"r_multiple": -1.0}] * 10)
    assert v["verdict"] == "insufficient"
    assert v["rollback_recommended"] is False


def test_stable_within_noise_band():
    flat = _series([0.3, 0.3, 0.3, 0.3, 0.3] * 12, 1)
    v = assess_learning_performance(flat)
    assert v["verdict"] == "stable"
    assert v["rollback_recommended"] is False
