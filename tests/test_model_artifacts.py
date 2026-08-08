from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.model_artifacts import ImmutableArtifactStore, ShadowArtifactGate
from core.model_governance import (
    PromotionDecision,
    PromotionPolicy,
    ProvenanceManifest,
    ShadowModelRegistry,
    ValidationArtifact,
    sha256_file,
)


def _manifest(candidate_id: str) -> ProvenanceManifest:
    return ProvenanceManifest(
        candidate_id=candidate_id,
        git_commit="a" * 40,
        dataset_id="dataset-v1",
        dataset_sha256="b" * 64,
        feature_set_id="features-v1",
        feature_fingerprint="c" * 64,
        random_seed=7,
    )


def _validation(candidate_id: str) -> ValidationArtifact:
    return ValidationArtifact(
        candidate_id=candidate_id,
        provenance=_manifest(candidate_id),
        data_source="mt5",
        has_spread_data=True,
        leakage_detected=False,
        feature_audit_passed=True,
        return_after_costs=0.07,
        profit_factor=1.30,
        sharpe=1.0,
        max_drawdown=0.05,
        trade_count=220,
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


def _source(tmp_path: Path, name: str, payload: bytes = b"model-v1") -> tuple[Path, Path]:
    root = tmp_path / name
    root.mkdir()
    model = root / "model.bin"
    model.write_bytes(payload)
    return root, model


def _ingest(store: ImmutableArtifactStore, tmp_path: Path, candidate_id: str, payload: bytes = b"model-v1") -> str:
    source, model = _source(tmp_path, candidate_id, payload)
    return store.ingest(
        candidate_id=candidate_id,
        source_dir=source,
        provenance=_manifest(candidate_id),
        validation=_validation(candidate_id),
        files=[model],
    )


def _register(registry: ShadowModelRegistry, tmp_path: Path, candidate_id: str, payload: bytes = b"model-v1") -> None:
    source = tmp_path / f"reg-{candidate_id}"
    source.mkdir()
    model = source / "model.bin"
    model.write_bytes(payload)
    registry.register_candidate(
        artifact_dir=source,
        manifest=_manifest(candidate_id),
        validation=_validation(candidate_id),
        model_files=[model],
    )


def test_ingest_is_content_addressed_and_reuses_identical_object(tmp_path: Path):
    store = ImmutableArtifactStore(tmp_path / "store")
    source, model = _source(tmp_path, "source")
    kwargs = dict(
        candidate_id="candidate-1",
        source_dir=source,
        provenance=_manifest("candidate-1"),
        validation=_validation("candidate-1"),
        files=[model],
    )
    first = store.ingest(**kwargs)
    second = store.ingest(**kwargs)
    assert first == second
    assert len(first) == 64
    assert store.verify(first).ok is True


def test_artifact_identity_changes_when_model_bytes_change(tmp_path: Path):
    store = ImmutableArtifactStore(tmp_path / "store")
    first = _ingest(store, tmp_path, "candidate-a", b"v1")
    second = _ingest(store, tmp_path, "candidate-b", b"v2")
    assert first != second


def test_tampered_payload_is_rejected_before_load(tmp_path: Path):
    store = ImmutableArtifactStore(tmp_path / "store")
    artifact_id = _ingest(store, tmp_path, "candidate-1")
    model = store.objects / artifact_id / "payload" / "model.bin"
    model.write_bytes(b"tampered")

    verification = store.verify(artifact_id)
    assert verification.ok is False
    assert any(item.startswith("sha256_mismatch:") or item.startswith("size_mismatch:") for item in verification.failures)
    with pytest.raises(PermissionError):
        store.prepare_load(artifact_id, "model.bin")


def test_tampered_manifest_is_rejected(tmp_path: Path):
    store = ImmutableArtifactStore(tmp_path / "store")
    artifact_id = _ingest(store, tmp_path, "candidate-1")
    manifest_path = store.objects / artifact_id / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["execution_authority_granted"] = True
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    verification = store.verify(artifact_id)
    assert verification.ok is False
    assert "unsafe_execution_authority" in verification.failures
    assert "descriptor_hash_mismatch" in verification.failures


def test_prepare_load_rejects_undeclared_or_traversal_path(tmp_path: Path):
    store = ImmutableArtifactStore(tmp_path / "store")
    artifact_id = _ingest(store, tmp_path, "candidate-1")
    with pytest.raises(FileNotFoundError):
        store.prepare_load(artifact_id, "other.bin")
    with pytest.raises(ValueError):
        store.prepare_load(artifact_id, "../model.bin")


def test_ingest_blocks_files_outside_declared_source(tmp_path: Path):
    store = ImmutableArtifactStore(tmp_path / "store")
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError, match="outside source_dir"):
        store.ingest(
            candidate_id="candidate-1",
            source_dir=source,
            provenance=_manifest("candidate-1"),
            validation=_validation("candidate-1"),
            files=[outside],
        )


def test_shadow_gate_stages_only_verified_matching_candidate(tmp_path: Path):
    store = ImmutableArtifactStore(tmp_path / "store")
    registry = ShadowModelRegistry(tmp_path / "registry")
    gate = ShadowArtifactGate(store)
    candidate = "candidate-1"
    artifact_id = _ingest(store, tmp_path, candidate)
    _register(registry, tmp_path, candidate)
    decision = PromotionPolicy().evaluate(_validation(candidate))

    state = gate.stage(
        registry=registry,
        candidate_id=candidate,
        artifact_id=artifact_id,
        decision=decision,
    )
    assert state["shadow_canary"] == candidate
    assert state["execution_authority_granted"] is False
    assert state["shadow_only"] is True


def test_shadow_gate_rejects_artifact_candidate_mismatch(tmp_path: Path):
    store = ImmutableArtifactStore(tmp_path / "store")
    registry = ShadowModelRegistry(tmp_path / "registry")
    gate = ShadowArtifactGate(store)
    artifact_id = _ingest(store, tmp_path, "candidate-a")
    _register(registry, tmp_path, "candidate-b")
    decision = PromotionPolicy().evaluate(_validation("candidate-b"))
    with pytest.raises(PermissionError, match="artifact/candidate identity mismatch"):
        gate.stage(
            registry=registry,
            candidate_id="candidate-b",
            artifact_id=artifact_id,
            decision=decision,
        )


def test_shadow_gate_rejects_unsafe_execution_decision(tmp_path: Path):
    store = ImmutableArtifactStore(tmp_path / "store")
    registry = ShadowModelRegistry(tmp_path / "registry")
    gate = ShadowArtifactGate(store)
    candidate = "candidate-1"
    artifact_id = _ingest(store, tmp_path, candidate)
    _register(registry, tmp_path, candidate)
    unsafe = PromotionDecision(
        candidate_id=candidate,
        eligible_for_shadow_canary=True,
        failures=(),
        shadow_only=False,
        execution_authority_granted=True,
    )
    with pytest.raises(PermissionError, match="unsafe promotion decision"):
        gate.stage(
            registry=registry,
            candidate_id=candidate,
            artifact_id=artifact_id,
            decision=unsafe,
        )


def test_rollback_is_shadow_only_and_append_audited(tmp_path: Path):
    store = ImmutableArtifactStore(tmp_path / "store")
    registry = ShadowModelRegistry(tmp_path / "registry")
    audit = tmp_path / "lifecycle.jsonl"
    gate = ShadowArtifactGate(store, audit_path=audit)

    a_id = _ingest(store, tmp_path, "candidate-a", b"a")
    b_id = _ingest(store, tmp_path, "candidate-b", b"b")
    _register(registry, tmp_path, "candidate-a", b"a")
    _register(registry, tmp_path, "candidate-b", b"b")
    a_decision = PromotionPolicy().evaluate(_validation("candidate-a"))
    b_decision = PromotionPolicy().evaluate(_validation("candidate-b"))

    gate.stage(registry=registry, candidate_id="candidate-a", artifact_id=a_id, decision=a_decision)
    gate.stage(registry=registry, candidate_id="candidate-b", artifact_id=b_id, decision=b_decision)
    state = gate.rollback(
        registry=registry,
        candidate_id="candidate-a",
        artifact_id=a_id,
        decision=a_decision,
    )

    assert state["shadow_canary"] == "candidate-a"
    assert state["execution_authority_granted"] is False
    rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert [row["action"] for row in rows] == ["stage", "stage", "rollback"]
    assert all(row["shadow_only"] is True for row in rows)
    assert all(row["execution_authority_granted"] is False for row in rows)


def test_prepare_load_rechecks_exact_file_integrity(tmp_path: Path):
    store = ImmutableArtifactStore(tmp_path / "store")
    artifact_id = _ingest(store, tmp_path, "candidate-1")
    path = store.prepare_load(artifact_id, "model.bin")
    assert path.is_file()
    manifest = store.manifest(artifact_id)
    assert sha256_file(path) == manifest["files"]["model.bin"]["sha256"]
