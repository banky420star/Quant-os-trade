from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.model_governance import PromotionPolicy, ProvenanceManifest
from research.validation.quant_harness import (
    QuantValidationConfig,
    QuantValidationHarness,
)


def _provenance(candidate_id: str = "challenger-001") -> ProvenanceManifest:
    return ProvenanceManifest(
        candidate_id=candidate_id,
        git_commit="a" * 40,
        dataset_id="xau-closed-trades-2026q3",
        dataset_sha256="b" * 64,
        feature_set_id="quant-feature-set-v1",
        feature_fingerprint="c" * 64,
        random_seed=17,
        created_at="2026-08-08T00:00:00+00:00",
    )


def _inputs() -> dict:
    index = pd.date_range("2025-01-01", periods=200, freq="h", tz="UTC")
    pattern = np.array([0.003, 0.003, 0.003, -0.001], dtype=float)
    net = pd.Series(np.tile(pattern, 50), index=index, name="net_return")
    gross = net + 0.0002
    rng = np.random.default_rng(1234)
    features = pd.DataFrame(
        {
            "momentum": rng.normal(size=len(index)),
            "volatility": rng.normal(size=len(index)),
            "spread_bps": rng.uniform(0.5, 2.0, size=len(index)),
        },
        index=index,
    )
    target = pd.Series(rng.normal(size=len(index)), index=index, name="target")
    regimes = pd.Series(
        ["trend"] * 100 + ["range"] * 100,
        index=index,
        name="regime",
    )
    return {
        "net_returns": net,
        "gross_returns": gross,
        "features": features,
        "target": target,
        "walk_forward_report": {
            "folds": 5,
            "positive_folds": 4,
            "median_oos_return": 0.04,
            "skipped_folds": 0,
        },
        "regimes": regimes,
        "random_policy_return": -0.01,
        "previous_champion_return": 0.10,
        "data_source": "mt5",
        "has_spread_data": True,
        "tests_passing": True,
        "account_telemetry_valid": True,
        "real_money_locked": True,
        "metadata": {"symbol": "XAUUSDm", "period": "2025"},
    }


def _harness(candidate_id: str = "challenger-001") -> QuantValidationHarness:
    return QuantValidationHarness(
        candidate_id=candidate_id,
        provenance=_provenance(candidate_id),
        config=QuantValidationConfig(
            lookahead_correlation_threshold=0.99,
            min_regime_observations=50,
            min_regimes_with_evidence=2,
        ),
    )


def test_complete_evidence_builds_shadow_only_promotable_bundle():
    bundle = _harness().evaluate(**_inputs())
    decision = bundle.promotion_decision(PromotionPolicy())
    assert decision.eligible_for_shadow_canary is True
    assert decision.failures == ()
    assert decision.shadow_only is True
    assert decision.execution_authority_granted is False
    assert bundle.shadow_only is True
    assert bundle.execution_authority_granted is False
    assert bundle.cost_stress_report["passed"] is True
    assert bundle.regime_breakdown["passed"] is True
    assert bundle.artifact.feature_audit_passed is True
    assert bundle.artifact.leakage_detected is False
    assert len(bundle.evidence_hash) == 64
    assert bundle.artifact.metadata["subjective_confidence"] is None


def test_evidence_hash_is_deterministic_for_identical_inputs():
    inputs = _inputs()
    first = _harness().evaluate(**inputs)
    second = _harness().evaluate(**inputs)
    assert first.evidence_hash == second.evidence_hash
    assert first.artifact.return_after_costs == second.artifact.return_after_costs


def test_missing_gross_returns_fails_cost_stress_and_promotion():
    inputs = _inputs()
    inputs["gross_returns"] = None
    bundle = _harness().evaluate(**inputs)
    assert bundle.cost_stress_report["passed"] is False
    assert bundle.cost_stress_report["reason"] == "gross_returns_missing"
    decision = bundle.promotion_decision()
    assert decision.eligible_for_shadow_canary is False
    assert "stress_test_failed" in decision.failures


def test_gross_net_alignment_mismatch_fails_closed():
    inputs = _inputs()
    inputs["gross_returns"] = inputs["gross_returns"].iloc[:-1]
    bundle = _harness().evaluate(**inputs)
    assert bundle.cost_stress_report["passed"] is False
    assert bundle.cost_stress_report["reason"] == "gross_net_alignment_mismatch"


def test_future_looking_feature_name_fails_point_in_time_audit():
    inputs = _inputs()
    features = inputs["features"].copy()
    features["future_return"] = inputs["target"]
    inputs["features"] = features
    bundle = _harness().evaluate(**inputs)
    assert bundle.point_in_time_report["passed"] is False
    assert bundle.artifact.feature_audit_passed is False
    decision = bundle.promotion_decision()
    assert "feature_audit_failed" in decision.failures


def test_missing_features_or_target_never_passes_feature_audit():
    inputs = _inputs()
    inputs["features"] = None
    inputs["target"] = None
    bundle = _harness().evaluate(**inputs)
    assert bundle.artifact.leakage_detected is True
    assert bundle.artifact.feature_audit_passed is False
    assert "features_or_target_missing" == bundle.leakage_report["note"]


def test_missing_baselines_fails_both_baseline_gates():
    inputs = _inputs()
    inputs["random_policy_return"] = None
    inputs["previous_champion_return"] = None
    bundle = _harness().evaluate(**inputs)
    decision = bundle.promotion_decision()
    assert "fails_random_baseline" in decision.failures
    assert "fails_previous_champion" in decision.failures


def test_insufficient_regime_evidence_fails_stress_gate():
    inputs = _inputs()
    inputs["regimes"] = pd.Series(
        ["only-one-regime"] * len(inputs["net_returns"]),
        index=inputs["net_returns"].index,
    )
    bundle = _harness().evaluate(**inputs)
    assert bundle.regime_breakdown["passed"] is False
    assert bundle.artifact.regime_breakdown_present is False
    assert bundle.artifact.stress_test_passed is False


def test_negative_observed_cost_is_rejected():
    inputs = _inputs()
    inputs["gross_returns"] = inputs["net_returns"] - 0.0001
    bundle = _harness().evaluate(**inputs)
    assert bundle.cost_stress_report["negative_observed_cost_count"] == 200
    assert bundle.cost_stress_report["passed"] is False


def test_bundle_persists_atomically_with_execution_authority_forced_off(tmp_path: Path):
    bundle = _harness().evaluate(**_inputs())
    target = bundle.write_json(tmp_path / "evidence" / "bundle.json")
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["shadow_only"] is True
    assert payload["execution_authority_granted"] is False
    assert payload["artifact"]["real_money_locked"] is True
    assert payload["evidence_hash"] == bundle.evidence_hash
    assert list(target.parent.glob("*.tmp")) == []


def test_candidate_provenance_mismatch_is_rejected():
    with pytest.raises(ValueError, match="candidate/provenance"):
        QuantValidationHarness(
            candidate_id="challenger-002",
            provenance=_provenance("challenger-001"),
        )


def test_invalid_config_is_rejected():
    with pytest.raises(ValueError, match="lookahead_correlation_threshold"):
        QuantValidationHarness(
            candidate_id="challenger-001",
            provenance=_provenance(),
            config=QuantValidationConfig(lookahead_correlation_threshold=2.0),
        )


def test_real_money_unlock_attestation_always_blocks_promotion():
    inputs = _inputs()
    inputs["real_money_locked"] = False
    bundle = _harness().evaluate(**inputs)
    decision = bundle.promotion_decision()
    assert decision.eligible_for_shadow_canary is False
    assert "real_money_not_locked" in decision.failures
    assert decision.execution_authority_granted is False


def test_empty_return_sample_is_rejected():
    inputs = _inputs()
    inputs["net_returns"] = pd.Series(dtype=float)
    with pytest.raises(ValueError, match="at least one finite observation"):
        _harness().evaluate(**inputs)
