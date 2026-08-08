"""Uncertainty-aware post-canary evidence policy for Quant OS.

This policy is the boundary after shadow champion/challenger observation.  It
may recommend that a challenger be presented to a human operator for research
review.  It cannot stage a live model, alter risk, open an execution gate, or
grant broker authority.

The policy is strategy-frequency-aware and evaluates paired realized outcomes:

* minimum observation count derived from expected signal frequency and days;
* minimum active market days and independently represented regimes;
* paired bootstrap confidence interval for challenger-minus-champion reward;
* outcome coverage, error rate, latency and drawdown guards;
* immutable evidence hash and atomic operator-review proposal.

A pass means only ``eligible_for_operator_review=True``.  Every decision forces
``shadow_only=True`` and ``execution_authority_granted=False``.
"""

from __future__ import annotations

import json
import math
import os
import random
import statistics
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.model_governance import fingerprint_payload


POST_CANARY_SCHEMA_VERSION = 1
_ALLOWED_ACTIONS = frozenset({"BUY", "SELL", "WAIT"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = min(max(float(quantile), 0.0), 1.0) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _bootstrap_mean_interval(
    values: Sequence[float],
    *,
    confidence_level: float,
    samples: int,
    seed: int,
) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    clean = [float(value) for value in values]
    observed = float(statistics.fmean(clean))
    if len(clean) == 1 or samples <= 1:
        return observed, observed, observed
    rng = random.Random(int(seed))
    n = len(clean)
    means: list[float] = []
    for _ in range(int(samples)):
        means.append(statistics.fmean(clean[rng.randrange(n)] for _ in range(n)))
    alpha = 1.0 - float(confidence_level)
    return observed, _percentile(means, alpha / 2.0), _percentile(means, 1.0 - alpha / 2.0)


def _max_cumulative_drawdown(rewards: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for reward in rewards:
        equity += float(reward)
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def _p95(values: Sequence[float]) -> float | None:
    clean = [float(value) for value in values if _finite(value)]
    return _percentile(clean, 0.95) if clean else None


@dataclass(frozen=True)
class StrategyFrequencyProfile:
    """Translate strategy cadence into a minimum shadow evidence requirement."""

    name: str
    expected_observations_per_day: float
    minimum_active_days: int
    minimum_coverage_fraction: float = 0.60
    absolute_minimum_observations: int = 100

    def validate(self) -> None:
        if not str(self.name or "").strip():
            raise ValueError("frequency profile name is required")
        if self.expected_observations_per_day <= 0:
            raise ValueError("expected_observations_per_day must be positive")
        if self.minimum_active_days < 1:
            raise ValueError("minimum_active_days must be positive")
        if not 0 < self.minimum_coverage_fraction <= 1:
            raise ValueError("minimum_coverage_fraction must be in (0, 1]")
        if self.absolute_minimum_observations < 1:
            raise ValueError("absolute_minimum_observations must be positive")

    @property
    def required_observations(self) -> int:
        self.validate()
        cadence_requirement = math.ceil(
            self.expected_observations_per_day
            * self.minimum_active_days
            * self.minimum_coverage_fraction
        )
        return max(int(self.absolute_minimum_observations), int(cadence_requirement))

    @classmethod
    def low_frequency(cls) -> "StrategyFrequencyProfile":
        return cls("low", 2.0, 40, 0.60, 60)

    @classmethod
    def medium_frequency(cls) -> "StrategyFrequencyProfile":
        return cls("medium", 8.0, 20, 0.60, 100)

    @classmethod
    def high_frequency(cls) -> "StrategyFrequencyProfile":
        return cls("high", 50.0, 10, 0.60, 300)


@dataclass(frozen=True)
class PostCanaryThresholds:
    min_independent_regimes: int = 2
    min_observations_per_regime: int = 20
    min_outcome_coverage: float = 0.95
    max_challenger_error_rate: float = 0.02
    min_challenger_total_reward: float = 0.0
    min_mean_reward_delta: float = 0.0
    confidence_level: float = 0.95
    bootstrap_samples: int = 2000
    bootstrap_seed: int = 20260808
    max_drawdown_degradation: float = 1.0
    max_challenger_p95_latency_ms: float = 5000.0

    def validate(self) -> None:
        if self.min_independent_regimes < 1:
            raise ValueError("min_independent_regimes must be positive")
        if self.min_observations_per_regime < 1:
            raise ValueError("min_observations_per_regime must be positive")
        if not 0 < self.min_outcome_coverage <= 1:
            raise ValueError("min_outcome_coverage must be in (0, 1]")
        if not 0 <= self.max_challenger_error_rate <= 1:
            raise ValueError("max_challenger_error_rate must be in [0, 1]")
        if not 0.50 < self.confidence_level < 1:
            raise ValueError("confidence_level must be in (0.50, 1)")
        if self.bootstrap_samples < 1:
            raise ValueError("bootstrap_samples must be positive")
        if self.max_drawdown_degradation < 0:
            raise ValueError("max_drawdown_degradation must be non-negative")
        if self.max_challenger_p95_latency_ms <= 0:
            raise ValueError("max_challenger_p95_latency_ms must be positive")


@dataclass(frozen=True)
class CanaryObservation:
    """One paired realized shadow outcome for the same market opportunity."""

    trace_id: str
    observed_at: str
    regime: str
    champion_reward: float | None
    challenger_reward: float | None
    champion_action: str = "WAIT"
    challenger_action: str = "WAIT"
    champion_error: bool = False
    challenger_error: bool = False
    champion_latency_ms: float | None = None
    challenger_latency_ms: float | None = None
    shadow_only: bool = True
    execution_authority_granted: bool = False

    def validate(self) -> tuple[str, ...]:
        failures: list[str] = []
        if not str(self.trace_id or "").strip():
            failures.append("missing_trace_id")
        try:
            _parse_timestamp(self.observed_at)
        except (TypeError, ValueError):
            failures.append("invalid_observed_at")
        if not str(self.regime or "").strip():
            failures.append("missing_regime")
        if str(self.champion_action).upper() not in _ALLOWED_ACTIONS:
            failures.append("invalid_champion_action")
        if str(self.challenger_action).upper() not in _ALLOWED_ACTIONS:
            failures.append("invalid_challenger_action")
        if self.champion_reward is not None and not _finite(self.champion_reward):
            failures.append("invalid_champion_reward")
        if self.challenger_reward is not None and not _finite(self.challenger_reward):
            failures.append("invalid_challenger_reward")
        for name, value in (
            ("champion_latency_ms", self.champion_latency_ms),
            ("challenger_latency_ms", self.challenger_latency_ms),
        ):
            if value is not None and (not _finite(value) or float(value) < 0):
                failures.append(f"invalid_{name}")
        if self.shadow_only is not True:
            failures.append("shadow_only_not_true")
        if self.execution_authority_granted is not False:
            failures.append("unsafe_execution_authority")
        return tuple(dict.fromkeys(failures))

    @property
    def paired_outcome_available(self) -> bool:
        return (
            not self.champion_error
            and not self.challenger_error
            and _finite(self.champion_reward)
            and _finite(self.challenger_reward)
        )

    @property
    def reward_delta(self) -> float | None:
        if not self.paired_outcome_available:
            return None
        return float(self.challenger_reward) - float(self.champion_reward)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OperatorReviewDecision:
    candidate_id: str
    champion_id: str
    eligible_for_operator_review: bool
    recommendation: str
    failures: tuple[str, ...]
    metrics: Mapping[str, Any]
    uncertainty: Mapping[str, Any]
    evidence_hash: str
    evaluated_at: str = field(default_factory=_utc_now)
    schema_version: int = POST_CANARY_SCHEMA_VERSION
    operator_review_required: bool = True
    live_promotion_eligible: bool = False
    shadow_only: bool = True
    execution_authority_granted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str)
        fd, tmp_name = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=str(target.parent), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass
        return target


class PostCanaryEvidencePolicy:
    """Fail-closed evidence gate ending at human operator review only."""

    def __init__(
        self,
        *,
        frequency: StrategyFrequencyProfile | None = None,
        thresholds: PostCanaryThresholds | None = None,
    ) -> None:
        self.frequency = frequency or StrategyFrequencyProfile.medium_frequency()
        self.thresholds = thresholds or PostCanaryThresholds()
        self.frequency.validate()
        self.thresholds.validate()

    def evaluate(
        self,
        *,
        candidate_id: str,
        champion_id: str,
        observations: Iterable[CanaryObservation],
    ) -> OperatorReviewDecision:
        if not str(candidate_id or "").strip():
            raise ValueError("candidate_id is required")
        if not str(champion_id or "").strip():
            raise ValueError("champion_id is required")
        if candidate_id == champion_id:
            raise ValueError("candidate_id and champion_id must differ")

        rows = list(observations)
        failures: list[str] = []
        invalid_details: list[dict[str, Any]] = []
        seen: set[str] = set()
        duplicate_trace_ids: list[str] = []
        for row in rows:
            if not isinstance(row, CanaryObservation):
                raise TypeError("observations must contain CanaryObservation values")
            row_failures = row.validate()
            if row_failures:
                invalid_details.append(
                    {"trace_id": row.trace_id, "failures": list(row_failures)}
                )
            if row.trace_id in seen:
                duplicate_trace_ids.append(row.trace_id)
            seen.add(row.trace_id)

        if invalid_details:
            failures.append("invalid_observations")
        if duplicate_trace_ids:
            failures.append("duplicate_trace_ids")

        valid_rows = [row for row in rows if not row.validate()]
        valid_rows.sort(key=lambda row: (_parse_timestamp(row.observed_at), row.trace_id))
        required_observations = self.frequency.required_observations
        if len(valid_rows) < required_observations:
            failures.append("insufficient_observations")

        active_days = {
            _parse_timestamp(row.observed_at).date().isoformat() for row in valid_rows
        }
        if len(active_days) < self.frequency.minimum_active_days:
            failures.append("insufficient_active_days")

        regime_counts: dict[str, int] = {}
        for row in valid_rows:
            key = str(row.regime)
            regime_counts[key] = regime_counts.get(key, 0) + 1
        qualifying_regimes = {
            key: count
            for key, count in regime_counts.items()
            if count >= self.thresholds.min_observations_per_regime
        }
        if len(qualifying_regimes) < self.thresholds.min_independent_regimes:
            failures.append("insufficient_regime_evidence")

        paired = [row for row in valid_rows if row.paired_outcome_available]
        outcome_coverage = len(paired) / len(valid_rows) if valid_rows else 0.0
        if outcome_coverage < self.thresholds.min_outcome_coverage:
            failures.append("insufficient_outcome_coverage")

        challenger_error_rate = (
            sum(bool(row.challenger_error) for row in valid_rows) / len(valid_rows)
            if valid_rows
            else 1.0
        )
        if challenger_error_rate > self.thresholds.max_challenger_error_rate:
            failures.append("challenger_error_rate_above_gate")

        champion_rewards = [float(row.champion_reward) for row in paired]
        challenger_rewards = [float(row.challenger_reward) for row in paired]
        deltas = [float(row.reward_delta) for row in paired if row.reward_delta is not None]
        challenger_total = sum(challenger_rewards)
        champion_total = sum(champion_rewards)
        if challenger_total <= self.thresholds.min_challenger_total_reward:
            failures.append("challenger_total_reward_below_gate")

        mean_delta, delta_lower, delta_upper = _bootstrap_mean_interval(
            deltas,
            confidence_level=self.thresholds.confidence_level,
            samples=self.thresholds.bootstrap_samples,
            seed=self.thresholds.bootstrap_seed,
        )
        if delta_lower <= self.thresholds.min_mean_reward_delta:
            failures.append("reward_delta_uncertainty_not_positive")

        champion_drawdown = _max_cumulative_drawdown(champion_rewards)
        challenger_drawdown = _max_cumulative_drawdown(challenger_rewards)
        if (
            challenger_drawdown - champion_drawdown
            > self.thresholds.max_drawdown_degradation
        ):
            failures.append("challenger_drawdown_degradation")

        challenger_latency = _p95(
            [row.challenger_latency_ms for row in valid_rows if row.challenger_latency_ms is not None]
        )
        champion_latency = _p95(
            [row.champion_latency_ms for row in valid_rows if row.champion_latency_ms is not None]
        )
        if (
            challenger_latency is not None
            and challenger_latency > self.thresholds.max_challenger_p95_latency_ms
        ):
            failures.append("challenger_latency_above_gate")

        conflict_count = sum(
            str(row.champion_action).upper() != str(row.challenger_action).upper()
            for row in valid_rows
        )
        unique_failures = tuple(dict.fromkeys(failures))

        metrics = {
            "observations_received": len(rows),
            "valid_observations": len(valid_rows),
            "required_observations": required_observations,
            "paired_outcomes": len(paired),
            "outcome_coverage": outcome_coverage,
            "active_days": len(active_days),
            "required_active_days": self.frequency.minimum_active_days,
            "regime_counts": regime_counts,
            "qualifying_regimes": qualifying_regimes,
            "challenger_error_rate": challenger_error_rate,
            "champion_total_reward": champion_total,
            "challenger_total_reward": challenger_total,
            "reward_delta_total": challenger_total - champion_total,
            "champion_max_cumulative_drawdown": champion_drawdown,
            "challenger_max_cumulative_drawdown": challenger_drawdown,
            "champion_p95_latency_ms": champion_latency,
            "challenger_p95_latency_ms": challenger_latency,
            "decision_conflicts": conflict_count,
            "invalid_observations": invalid_details,
            "duplicate_trace_ids": sorted(set(duplicate_trace_ids)),
            "frequency_profile": asdict(self.frequency),
            "thresholds": asdict(self.thresholds),
        }
        uncertainty = {
            "method": "paired_bootstrap_mean_delta",
            "confidence_level": self.thresholds.confidence_level,
            "bootstrap_samples": self.thresholds.bootstrap_samples,
            "mean_reward_delta": mean_delta,
            "lower_bound": delta_lower,
            "upper_bound": delta_upper,
            "paired_sample_size": len(deltas),
            "seed": self.thresholds.bootstrap_seed,
        }
        evidence_payload = {
            "candidate_id": candidate_id,
            "champion_id": champion_id,
            "observations": [row.to_dict() for row in valid_rows],
            "metrics": metrics,
            "uncertainty": uncertainty,
            "failures": unique_failures,
            "shadow_only": True,
            "execution_authority_granted": False,
        }
        evidence_hash = fingerprint_payload(evidence_payload)
        eligible = not unique_failures
        return OperatorReviewDecision(
            candidate_id=candidate_id,
            champion_id=champion_id,
            eligible_for_operator_review=eligible,
            recommendation=(
                "PRESENT_FOR_OPERATOR_RESEARCH_REVIEW"
                if eligible
                else "KEEP_CURRENT_RESEARCH_CHAMPION"
            ),
            failures=unique_failures,
            metrics=metrics,
            uncertainty=uncertainty,
            evidence_hash=evidence_hash,
        )


__all__ = [
    "POST_CANARY_SCHEMA_VERSION",
    "CanaryObservation",
    "OperatorReviewDecision",
    "PostCanaryEvidencePolicy",
    "PostCanaryThresholds",
    "StrategyFrequencyProfile",
]
