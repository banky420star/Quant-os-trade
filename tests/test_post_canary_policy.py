from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.post_canary_policy import (
    CanaryObservation,
    PostCanaryEvidencePolicy,
    PostCanaryThresholds,
    StrategyFrequencyProfile,
)


def _profile() -> StrategyFrequencyProfile:
    return StrategyFrequencyProfile(
        name="test-medium",
        expected_observations_per_day=5.0,
        minimum_active_days=10,
        minimum_coverage_fraction=0.5,
        absolute_minimum_observations=60,
    )


def _thresholds(**overrides) -> PostCanaryThresholds:
    values = {
        "min_independent_regimes": 3,
        "min_observations_per_regime": 15,
        "min_outcome_coverage": 0.95,
        "max_challenger_error_rate": 0.02,
        "min_challenger_total_reward": 0.0,
        "min_mean_reward_delta": 0.0,
        "confidence_level": 0.95,
        "bootstrap_samples": 300,
        "bootstrap_seed": 42,
        "max_drawdown_degradation": 1.0,
        "max_challenger_p95_latency_ms": 100.0,
    }
    values.update(overrides)
    return PostCanaryThresholds(**values)


def _policy(**threshold_overrides) -> PostCanaryEvidencePolicy:
    return PostCanaryEvidencePolicy(
        frequency=_profile(),
        thresholds=_thresholds(**threshold_overrides),
    )


def _observations(
    *,
    count: int = 90,
    delta: float = 0.05,
    challenger_error_indices: set[int] | None = None,
    missing_reward_indices: set[int] | None = None,
    days: int = 15,
    regimes: tuple[str, ...] = ("trend", "range", "volatile"),
    challenger_latency_ms: float = 20.0,
) -> list[CanaryObservation]:
    challenger_error_indices = challenger_error_indices or set()
    missing_reward_indices = missing_reward_indices or set()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows: list[CanaryObservation] = []
    for index in range(count):
        day = index % max(days, 1)
        observed_at = start + timedelta(days=day, minutes=index)
        regime = regimes[index % len(regimes)]
        champion_reward = 0.02 if index % 5 != 0 else -0.01
        challenger_reward = champion_reward + delta
        if index in missing_reward_indices:
            challenger_reward = None
        challenger_error = index in challenger_error_indices
        rows.append(
            CanaryObservation(
                trace_id=f"trace-{index:04d}",
                observed_at=observed_at.isoformat(),
                regime=regime,
                champion_reward=champion_reward,
                challenger_reward=challenger_reward,
                champion_action="BUY" if index % 2 == 0 else "WAIT",
                challenger_action="BUY" if index % 3 else "SELL",
                champion_error=False,
                challenger_error=challenger_error,
                champion_latency_ms=10.0,
                challenger_latency_ms=challenger_latency_ms,
            )
        )
    return rows


def test_frequency_profile_derives_strategy_aware_sample_requirement():
    low = StrategyFrequencyProfile.low_frequency()
    medium = StrategyFrequencyProfile.medium_frequency()
    high = StrategyFrequencyProfile.high_frequency()
    assert low.required_observations == 60
    assert medium.required_observations == 100
    assert high.required_observations == 300
    assert low.minimum_active_days > high.minimum_active_days


def test_strong_paired_evidence_reaches_operator_review_only():
    decision = _policy().evaluate(
        candidate_id="challenger-001",
        champion_id="champion-001",
        observations=_observations(),
    )
    assert decision.eligible_for_operator_review is True
    assert decision.recommendation == "PRESENT_FOR_OPERATOR_RESEARCH_REVIEW"
    assert decision.failures == ()
    assert decision.operator_review_required is True
    assert decision.live_promotion_eligible is False
    assert decision.shadow_only is True
    assert decision.execution_authority_granted is False
    assert decision.uncertainty["lower_bound"] > 0
    assert decision.metrics["qualifying_regimes"] == {
        "range": 30,
        "trend": 30,
        "volatile": 30,
    }


def test_insufficient_sample_and_active_days_fail_closed():
    decision = _policy().evaluate(
        candidate_id="challenger",
        champion_id="champion",
        observations=_observations(count=30, days=5),
    )
    assert decision.eligible_for_operator_review is False
    assert "insufficient_observations" in decision.failures
    assert "insufficient_active_days" in decision.failures
    assert decision.execution_authority_granted is False


def test_insufficient_independent_regimes_fails():
    decision = _policy().evaluate(
        candidate_id="challenger",
        champion_id="champion",
        observations=_observations(regimes=("trend",)),
    )
    assert "insufficient_regime_evidence" in decision.failures


def test_duplicate_trace_ids_are_rejected():
    rows = _observations()
    rows[-1] = replace(rows[-1], trace_id=rows[0].trace_id)
    decision = _policy().evaluate(
        candidate_id="challenger",
        champion_id="champion",
        observations=rows,
    )
    assert "duplicate_trace_ids" in decision.failures
    assert rows[0].trace_id in decision.metrics["duplicate_trace_ids"]


def test_missing_paired_outcomes_reduce_coverage_and_fail():
    rows = _observations(missing_reward_indices=set(range(10)))
    decision = _policy().evaluate(
        candidate_id="challenger",
        champion_id="champion",
        observations=rows,
    )
    assert decision.metrics["outcome_coverage"] < 0.95
    assert "insufficient_outcome_coverage" in decision.failures


def test_challenger_errors_are_counted_and_gated():
    rows = _observations(challenger_error_indices=set(range(6)))
    decision = _policy().evaluate(
        candidate_id="challenger",
        champion_id="champion",
        observations=rows,
    )
    assert decision.metrics["challenger_error_rate"] > 0.02
    assert "challenger_error_rate_above_gate" in decision.failures
    assert "insufficient_outcome_coverage" in decision.failures


def test_uncertain_or_negative_delta_does_not_pass():
    rows = _observations(delta=0.0)
    decision = _policy().evaluate(
        candidate_id="challenger",
        champion_id="champion",
        observations=rows,
    )
    assert decision.uncertainty["lower_bound"] <= 0
    assert "reward_delta_uncertainty_not_positive" in decision.failures


def test_negative_total_challenger_reward_fails():
    rows = [
        replace(row, champion_reward=-0.10, challenger_reward=-0.05)
        for row in _observations()
    ]
    decision = _policy().evaluate(
        candidate_id="challenger",
        champion_id="champion",
        observations=rows,
    )
    assert decision.metrics["challenger_total_reward"] < 0
    assert "challenger_total_reward_below_gate" in decision.failures


def test_drawdown_degradation_guard_can_reject_positive_mean():
    rows = _observations(delta=0.05)
    modified: list[CanaryObservation] = []
    for index, row in enumerate(rows):
        challenger_reward = 0.25 if index < 45 else -0.15
        modified.append(replace(row, challenger_reward=challenger_reward))
    decision = _policy(max_drawdown_degradation=0.1).evaluate(
        candidate_id="challenger",
        champion_id="champion",
        observations=modified,
    )
    assert decision.metrics["challenger_max_cumulative_drawdown"] > decision.metrics[
        "champion_max_cumulative_drawdown"
    ]
    assert "challenger_drawdown_degradation" in decision.failures


def test_latency_guard_rejects_slow_challenger():
    decision = _policy().evaluate(
        candidate_id="challenger",
        champion_id="champion",
        observations=_observations(challenger_latency_ms=250.0),
    )
    assert decision.metrics["challenger_p95_latency_ms"] == 250.0
    assert "challenger_latency_above_gate" in decision.failures


def test_invalid_or_unsafe_observation_is_rejected():
    rows = _observations()
    rows[0] = replace(
        rows[0],
        challenger_action="MAX_LEVERAGE",
        shadow_only=False,
        execution_authority_granted=True,
    )
    decision = _policy().evaluate(
        candidate_id="challenger",
        champion_id="champion",
        observations=rows,
    )
    assert "invalid_observations" in decision.failures
    detail = decision.metrics["invalid_observations"][0]
    assert "invalid_challenger_action" in detail["failures"]
    assert "shadow_only_not_true" in detail["failures"]
    assert "unsafe_execution_authority" in detail["failures"]
    assert decision.execution_authority_granted is False


def test_decision_evidence_is_deterministic_and_atomic(tmp_path: Path):
    rows = _observations()
    first = _policy().evaluate(
        candidate_id="challenger",
        champion_id="champion",
        observations=rows,
    )
    second = _policy().evaluate(
        candidate_id="challenger",
        champion_id="champion",
        observations=list(reversed(rows)),
    )
    assert first.evidence_hash == second.evidence_hash

    target = first.write_json(tmp_path / "review" / "proposal.json")
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["eligible_for_operator_review"] is True
    assert payload["operator_review_required"] is True
    assert payload["live_promotion_eligible"] is False
    assert payload["execution_authority_granted"] is False
    assert list(target.parent.glob("*.tmp")) == []


def test_candidate_and_champion_ids_must_differ():
    with pytest.raises(ValueError, match="must differ"):
        _policy().evaluate(
            candidate_id="same",
            champion_id="same",
            observations=_observations(),
        )


def test_invalid_thresholds_fail_at_policy_construction():
    with pytest.raises(ValueError, match="confidence_level"):
        PostCanaryEvidencePolicy(
            frequency=_profile(),
            thresholds=_thresholds(confidence_level=1.0),
        )
