from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core.model_governance import (
    PromotionDecision,
    PromotionPolicy,
    ProvenanceManifest,
    ShadowModelRegistry,
    ValidationArtifact,
    fingerprint_feature_matrix,
    fingerprint_payload,
    sha256_file,
)


def _manifest(candidate_id: str = "candidate-001") -> ProvenanceManifest:
    return ProvenanceManifest(
        candidate_id=candidate_id,
        git_commit="a" * 40,
        dataset_id="xau-m1-2026q3",
        dataset_sha256="b" * 64,
        feature_set_id="m1-structure-v1",
        feature_fingerprint="c" * 64,
        random_seed=42,
    )


def _valid_artifact(candidate_id: str = "candidate-001") -> ValidationArtifact:
    return ValidationArtifact(
        candidate_id=candidate_id,
        provenance=_manifest(candidate_id),
        data_source="mt5",
        has_spread_data=True,
        leakage_detected=False,
        feature_audit_passed=True,
        return_after_costs=0.08,
        profit_factor=1.35,
        sharpe=1.1,
        max_drawdown=0.06,
        trade_count=250,
        max_single_trade_profit_share=0.10,
        walk_forward_windows_passed=5,
        regime_breakdown_present=True,
        stress_test_passed=True,
        beats_random_policy=True,
        beats_previous_champion=True,
        tests_passing=True,
        account_telemetry_valid=True,
        real_money_locked=True,
    )


def test_fingerprint_payload_is_stable_and_content_addressed():
    a = fingerprint_payload({"b": 2, "a": [1, 2, 3]})
    b = fingerprint_payload({"a": [1, 2, 3], "b": 2})
    c = fingerprint_payload({"a": [1, 2, 4], "b": 2})
    assert a == b
    assert a != c
    assert len(a) == 64


def test_feature_ablation_fingerprint_changes_with_matrix():
    control = [[1.0, 2.0], [3.0, 4.0]]
    ablated = [[1.0, 0.0], [3.0, 0.0]]
    assert fingerprint_feature_matrix(control) != fingerprint_feature_matrix(ablated)


def test_provenance_manifest_fails_closed_on_missing_or_invalid_hashes():
    bad = replace(_manifest(), dataset_id="", dataset_sha256="not-a-hash")
    failures = bad.validate()
    assert "missing_dataset_id" in failures
    assert "invalid_dataset_sha256" in failures


def test_valid_candidate_only_becomes_shadow_canary_eligible():
    decision = PromotionPolicy().evaluate(_valid_artifact())
    assert decision.eligible_for_shadow_canary is True
    assert decision.failures == ()
    assert decision.shadow_only is True
    assert decision.execution_authority_granted is False


@pytest.mark.parametrize(
    ("field", "value", "expected_failure"),
    [
        ("data_source", "csv", "data_source_not_mt5"),
        ("has_spread_data", False, "missing_spread_data"),
        ("leakage_detected", True, "data_leakage_detected"),
        ("feature_audit_passed", False, "feature_audit_failed"),
        ("return_after_costs", -0.01, "nonpositive_oos_return_after_costs"),
        ("profit_factor", 0.9, "profit_factor_below_gate"),
        ("sharpe", 0.1, "sharpe_below_gate"),
        ("max_drawdown", 0.25, "drawdown_above_gate"),
        ("trade_count", 20, "insufficient_trade_count"),
        ("max_single_trade_profit_share", 0.4, "single_trade_concentration"),
        ("walk_forward_windows_passed", 1, "insufficient_walk_forward_windows"),
        ("regime_breakdown_present", False, "missing_regime_breakdown"),
        ("stress_test_passed", False, "stress_test_failed"),
        ("beats_random_policy", False, "fails_random_baseline"),
        ("beats_previous_champion", False, "fails_previous_champion"),
        ("tests_passing", False, "tests_not_passing"),
        ("account_telemetry_valid", False, "account_telemetry_invalid"),
        ("real_money_locked", False, "real_money_not_locked"),
    ],
)
def test_promotion_policy_is_fail_closed(field, value, expected_failure):
    artifact = replace(_valid_artifact(), **{field: value})
    decision = PromotionPolicy().evaluate(artifact)
    assert decision.eligible_for_shadow_canary is False
    assert expected_failure in decision.failures
    assert decision.execution_authority_granted is False


def test_candidate_id_mismatch_is_rejected():
    artifact = replace(_valid_artifact(), candidate_id="other")
    decision = PromotionPolicy().evaluate(artifact)
    assert decision.eligible_for_shadow_canary is False
    assert "candidate_id_mismatch" in decision.failures


def test_registry_records_integrity_and_never_execution_authority(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    model = artifacts / "model.bin"
    model.write_bytes(b"model-v1")

    registry = ShadowModelRegistry(tmp_path / "registry")
    record_dir = registry.register_candidate(
        artifact_dir=artifacts,
        manifest=_manifest(),
        validation=_valid_artifact(),
        model_files=[model],
    )
    assert record_dir.exists()
    text = (record_dir / "record.json").read_text(encoding="utf-8")
    assert sha256_file(model) in text
    assert '"shadow_only": true' in text
    assert '"execution_authority_granted": false' in text

    decision = PromotionPolicy().evaluate(_valid_artifact())
    state = registry.stage_shadow_canary("candidate-001", decision)
    assert state["shadow_canary"] == "candidate-001"
    assert state["shadow_only"] is True
    assert state["execution_authority_granted"] is False


def test_registry_rejects_failed_candidate(tmp_path: Path):
    registry = ShadowModelRegistry(tmp_path / "registry")
    failed = PromotionDecision(
        candidate_id="candidate-001",
        eligible_for_shadow_canary=False,
        failures=("stress_test_failed",),
    )
    with pytest.raises(PermissionError):
        registry.stage_shadow_canary("candidate-001", failed)


def test_registry_rejects_any_decision_claiming_execution_authority(tmp_path: Path):
    registry = ShadowModelRegistry(tmp_path / "registry")
    unsafe = PromotionDecision(
        candidate_id="candidate-001",
        eligible_for_shadow_canary=True,
        failures=(),
        shadow_only=False,
        execution_authority_granted=True,
    )
    with pytest.raises(PermissionError):
        registry.stage_shadow_canary("candidate-001", unsafe)


def test_registry_blocks_artifacts_outside_declared_root(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"not-in-artifact-root")

    registry = ShadowModelRegistry(tmp_path / "registry")
    with pytest.raises(ValueError, match="outside artifact_dir"):
        registry.register_candidate(
            artifact_dir=artifacts,
            manifest=_manifest(),
            validation=_valid_artifact(),
            model_files=[outside],
        )
