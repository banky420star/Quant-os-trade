"""Standardized, fail-closed research validation for Quant OS challengers.

The harness converts explicit research evidence into the canonical
``ValidationArtifact`` consumed by ``PromotionPolicy``.  It does not train a
model, assign subjective confidence, stage a canary, mutate configuration, or
import any execution/broker code.

Inputs are deliberately explicit:

* one closed-observation net-return series;
* the corresponding gross-return series so doubled-cost stress is measurable;
* point-in-time feature data and a target for leakage checks;
* purged walk-forward evidence;
* regime labels;
* random-policy and previous-champion baselines;
* test, telemetry and real-money-lock attestations.

Missing evidence fails closed rather than being replaced by optimistic defaults.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from core.model_governance import (
    PromotionDecision,
    PromotionPolicy,
    ProvenanceManifest,
    ValidationArtifact,
    fingerprint_payload,
)
from research.validation.lookahead_check import detect_lookahead, point_in_time_audit


VALIDATION_BUNDLE_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_numeric(series: pd.Series | None, *, name: str) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float, name=name)
    if not isinstance(series, pd.Series):
        raise TypeError(f"{name} must be a pandas Series")
    cleaned = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    cleaned.name = name
    return cleaned.astype(float)


def _compounded_return(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    return float((1.0 + returns).prod() - 1.0)


def _max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return 1.0
    equity = (1.0 + returns).cumprod()
    peaks = equity.cummax()
    drawdown = equity / peaks - 1.0
    return abs(float(drawdown.min()))


def _profit_factor(returns: pd.Series) -> float:
    gross_profit = float(returns[returns > 0].sum())
    gross_loss = abs(float(returns[returns < 0].sum()))
    if gross_loss == 0.0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _sample_sharpe(returns: pd.Series) -> float:
    if len(returns) < 2:
        return 0.0
    std = float(returns.std(ddof=1))
    if std <= 0.0:
        return 0.0
    # Observation-level Sharpe.  We deliberately avoid inventing an annual
    # frequency for heterogeneous trade samples.
    return float(returns.mean() / std * math.sqrt(len(returns)))


def _max_single_trade_profit_share(returns: pd.Series) -> float:
    winners = returns[returns > 0]
    total = float(winners.sum())
    if total <= 0.0:
        return 1.0
    return float(winners.max() / total)


def _regime_breakdown(returns: pd.Series, regimes: pd.Series | None) -> dict[str, Any]:
    if regimes is None or not isinstance(regimes, pd.Series):
        return {}
    aligned = pd.DataFrame({"return": returns, "regime": regimes.reindex(returns.index)}).dropna()
    if aligned.empty:
        return {}
    result: dict[str, Any] = {}
    for raw_name, group in aligned.groupby("regime", sort=True):
        name = str(raw_name)
        values = group["return"].astype(float)
        result[name] = {
            "observations": int(len(values)),
            "total_return": _compounded_return(values),
            "mean_return": float(values.mean()),
            "win_rate": float((values > 0).mean()),
            "max_drawdown": _max_drawdown(values),
        }
    return result


@dataclass(frozen=True)
class QuantValidationConfig:
    lookahead_max_lag: int = 20
    lookahead_correlation_threshold: float = 0.15
    min_walk_forward_windows: int = 3
    min_regime_observations: int = 10
    min_regimes_with_evidence: int = 2
    require_positive_double_cost_return: bool = True
    max_double_cost_degradation_fraction: float = 0.80

    def validate(self) -> None:
        if self.lookahead_max_lag < 1:
            raise ValueError("lookahead_max_lag must be positive")
        if not 0 < self.lookahead_correlation_threshold <= 1:
            raise ValueError("lookahead_correlation_threshold must be in (0, 1]")
        if self.min_walk_forward_windows < 1:
            raise ValueError("min_walk_forward_windows must be positive")
        if self.min_regime_observations < 1:
            raise ValueError("min_regime_observations must be positive")
        if self.min_regimes_with_evidence < 1:
            raise ValueError("min_regimes_with_evidence must be positive")
        if not 0 <= self.max_double_cost_degradation_fraction <= 1:
            raise ValueError("max_double_cost_degradation_fraction must be in [0, 1]")


@dataclass(frozen=True)
class ValidationBundle:
    artifact: ValidationArtifact
    summary_metrics: Mapping[str, Any]
    point_in_time_report: Mapping[str, Any]
    leakage_report: Mapping[str, Any]
    walk_forward_report: Mapping[str, Any]
    cost_stress_report: Mapping[str, Any]
    regime_breakdown: Mapping[str, Any]
    baseline_comparison: Mapping[str, Any]
    evidence_hash: str
    generated_at: str = field(default_factory=_utc_now)
    schema_version: int = VALIDATION_BUNDLE_SCHEMA_VERSION
    shadow_only: bool = True
    execution_authority_granted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def promotion_decision(self, policy: PromotionPolicy | None = None) -> PromotionDecision:
        return (policy or PromotionPolicy()).evaluate(self.artifact)

    def write_json(self, path: str | Path) -> Path:
        """Persist atomically; never write partial validation evidence."""
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


class QuantValidationHarness:
    """Build deterministic validation evidence for one research candidate."""

    def __init__(
        self,
        *,
        candidate_id: str,
        provenance: ProvenanceManifest,
        config: QuantValidationConfig | None = None,
    ) -> None:
        if not candidate_id:
            raise ValueError("candidate_id is required")
        if candidate_id != provenance.candidate_id:
            raise ValueError("candidate/provenance candidate_id mismatch")
        failures = provenance.validate()
        if failures:
            raise ValueError("invalid provenance: " + ",".join(failures))
        self.candidate_id = candidate_id
        self.provenance = provenance
        self.config = config or QuantValidationConfig()
        self.config.validate()

    def evaluate(
        self,
        *,
        net_returns: pd.Series,
        gross_returns: pd.Series | None,
        features: pd.DataFrame | None,
        target: pd.Series | None,
        walk_forward_report: Mapping[str, Any] | None,
        regimes: pd.Series | None,
        random_policy_return: float | None,
        previous_champion_return: float | None,
        data_source: str = "mt5",
        has_spread_data: bool = False,
        tests_passing: bool = False,
        account_telemetry_valid: bool = False,
        real_money_locked: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> ValidationBundle:
        net = _clean_numeric(net_returns, name="net_returns")
        gross = _clean_numeric(gross_returns, name="gross_returns")
        if net.empty:
            raise ValueError("net_returns must contain at least one finite observation")

        summary = {
            "return_after_costs": _compounded_return(net),
            "profit_factor": _profit_factor(net),
            "sharpe": _sample_sharpe(net),
            "max_drawdown": _max_drawdown(net),
            "trade_count": int(len(net)),
            "max_single_trade_profit_share": _max_single_trade_profit_share(net),
            "wins": int((net > 0).sum()),
            "losses": int((net < 0).sum()),
            "mean_return": float(net.mean()),
            "observation_unit": "closed_trade_or_closed_validation_observation",
        }

        if features is None:
            point_report: dict[str, Any] = {
                "passed": False,
                "issues": [{"type": "features_missing"}],
            }
        elif not isinstance(features, pd.DataFrame):
            raise TypeError("features must be a pandas DataFrame")
        else:
            point_report = point_in_time_audit(features)

        if features is None or target is None:
            leakage_report: dict[str, Any] = {
                "passed": False,
                "suspicious_features": [],
                "note": "features_or_target_missing",
            }
        else:
            if not isinstance(target, pd.Series):
                raise TypeError("target must be a pandas Series")
            leakage_report = detect_lookahead(
                features,
                target,
                max_lag=self.config.lookahead_max_lag,
                correlation_threshold=self.config.lookahead_correlation_threshold,
            )

        feature_audit_passed = bool(point_report.get("passed")) and bool(
            leakage_report.get("passed")
        )
        leakage_detected = not bool(leakage_report.get("passed"))

        walk = dict(walk_forward_report or {})
        windows_passed = int(
            walk.get("positive_folds")
            or walk.get("windows_passed")
            or walk.get("walk_forward_windows_passed")
            or 0
        )
        windows_evaluated = int(walk.get("folds") or walk.get("windows") or 0)
        walk["windows_passed_normalized"] = windows_passed
        walk["windows_evaluated_normalized"] = windows_evaluated
        walk["minimum_required"] = self.config.min_walk_forward_windows

        cost_stress: dict[str, Any]
        if gross.empty:
            cost_stress = {
                "passed": False,
                "reason": "gross_returns_missing",
                "double_cost_return": None,
            }
        else:
            common = net.index.intersection(gross.index)
            if len(common) != len(net):
                cost_stress = {
                    "passed": False,
                    "reason": "gross_net_alignment_mismatch",
                    "aligned_observations": int(len(common)),
                    "expected_observations": int(len(net)),
                    "double_cost_return": None,
                }
            else:
                net_aligned = net.reindex(common)
                gross_aligned = gross.reindex(common)
                observed_cost = gross_aligned - net_aligned
                negative_cost_count = int((observed_cost < -1e-12).sum())
                double_cost_returns = gross_aligned - 2.0 * observed_cost
                double_total = _compounded_return(double_cost_returns)
                base_total = summary["return_after_costs"]
                if base_total > 0:
                    degradation = max(0.0, (base_total - double_total) / base_total)
                else:
                    degradation = 1.0
                survives = (
                    double_total > 0
                    if self.config.require_positive_double_cost_return
                    else True
                )
                passed = (
                    negative_cost_count == 0
                    and survives
                    and degradation <= self.config.max_double_cost_degradation_fraction
                )
                cost_stress = {
                    "passed": passed,
                    "base_return_after_costs": base_total,
                    "double_cost_return": double_total,
                    "degradation_fraction": degradation,
                    "negative_observed_cost_count": negative_cost_count,
                    "observations": int(len(common)),
                }

        regime_report = _regime_breakdown(net, regimes)
        qualifying_regimes = sum(
            int(payload.get("observations", 0)) >= self.config.min_regime_observations
            for payload in regime_report.values()
        )
        regime_report = {
            "regimes": regime_report,
            "qualifying_regimes": qualifying_regimes,
            "minimum_regimes_required": self.config.min_regimes_with_evidence,
            "minimum_observations_per_regime": self.config.min_regime_observations,
            "passed": qualifying_regimes >= self.config.min_regimes_with_evidence,
        }

        baseline = {
            "candidate_return_after_costs": summary["return_after_costs"],
            "random_policy_return": random_policy_return,
            "previous_champion_return": previous_champion_return,
            "beats_random_policy": (
                random_policy_return is not None
                and summary["return_after_costs"] > float(random_policy_return)
            ),
            "beats_previous_champion": (
                previous_champion_return is not None
                and summary["return_after_costs"] > float(previous_champion_return)
            ),
        }

        stress_test_passed = bool(cost_stress.get("passed")) and bool(
            regime_report.get("passed")
        )
        artifact_metadata = {
            **dict(metadata or {}),
            "validation_bundle_schema_version": VALIDATION_BUNDLE_SCHEMA_VERSION,
            "windows_evaluated": windows_evaluated,
            "double_cost_stress": cost_stress,
            "point_in_time_report": point_report,
            "leakage_report": leakage_report,
            "regime_summary": {
                "qualifying_regimes": qualifying_regimes,
                "minimum_required": self.config.min_regimes_with_evidence,
            },
            "subjective_confidence": None,
            "shadow_only": True,
            "execution_authority_granted": False,
        }

        artifact = ValidationArtifact(
            candidate_id=self.candidate_id,
            provenance=self.provenance,
            data_source=str(data_source),
            has_spread_data=bool(has_spread_data),
            leakage_detected=leakage_detected,
            feature_audit_passed=feature_audit_passed,
            return_after_costs=float(summary["return_after_costs"]),
            profit_factor=float(summary["profit_factor"]),
            sharpe=float(summary["sharpe"]),
            max_drawdown=float(summary["max_drawdown"]),
            trade_count=int(summary["trade_count"]),
            max_single_trade_profit_share=float(
                summary["max_single_trade_profit_share"]
            ),
            walk_forward_windows_passed=windows_passed,
            regime_breakdown_present=bool(regime_report.get("passed")),
            stress_test_passed=stress_test_passed,
            beats_random_policy=bool(baseline["beats_random_policy"]),
            beats_previous_champion=bool(baseline["beats_previous_champion"]),
            tests_passing=bool(tests_passing),
            account_telemetry_valid=bool(account_telemetry_valid),
            real_money_locked=bool(real_money_locked),
            metadata=artifact_metadata,
        )

        evidence_payload = {
            "schema_version": VALIDATION_BUNDLE_SCHEMA_VERSION,
            "candidate_id": self.candidate_id,
            "provenance": asdict(self.provenance),
            "summary_metrics": summary,
            "point_in_time_report": point_report,
            "leakage_report": leakage_report,
            "walk_forward_report": walk,
            "cost_stress_report": cost_stress,
            "regime_breakdown": regime_report,
            "baseline_comparison": baseline,
            "attestations": {
                "data_source": str(data_source),
                "has_spread_data": bool(has_spread_data),
                "tests_passing": bool(tests_passing),
                "account_telemetry_valid": bool(account_telemetry_valid),
                "real_money_locked": bool(real_money_locked),
            },
            "shadow_only": True,
            "execution_authority_granted": False,
        }
        evidence_hash = fingerprint_payload(evidence_payload)

        return ValidationBundle(
            artifact=artifact,
            summary_metrics=summary,
            point_in_time_report=point_report,
            leakage_report=leakage_report,
            walk_forward_report=walk,
            cost_stress_report=cost_stress,
            regime_breakdown=regime_report,
            baseline_comparison=baseline,
            evidence_hash=evidence_hash,
        )


__all__ = [
    "QuantValidationConfig",
    "QuantValidationHarness",
    "ValidationBundle",
]
