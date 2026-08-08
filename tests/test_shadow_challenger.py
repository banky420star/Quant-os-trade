from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.model_artifacts import ImmutableArtifactStore
from core.model_governance import ProvenanceManifest, ValidationArtifact
from core.shadow_challenger import (
    ShadowChallengerService,
    ShadowMarketSnapshot,
    ShadowParticipant,
)


def _manifest(candidate_id: str) -> ProvenanceManifest:
    return ProvenanceManifest(
        candidate_id=candidate_id,
        git_commit="a" * 40,
        dataset_id="shared-shadow-dataset",
        dataset_sha256="b" * 64,
        feature_set_id="shared-shadow-features",
        feature_fingerprint="c" * 64,
        random_seed=42,
        created_at="2026-08-08T00:00:00+00:00",
    )


def _validation(candidate_id: str) -> ValidationArtifact:
    return ValidationArtifact(
        candidate_id=candidate_id,
        provenance=_manifest(candidate_id),
        data_source="mt5",
        has_spread_data=True,
        leakage_detected=False,
        feature_audit_passed=True,
        return_after_costs=0.05,
        profit_factor=1.3,
        sharpe=1.0,
        max_drawdown=0.05,
        trade_count=200,
        max_single_trade_profit_share=0.10,
        walk_forward_windows_passed=4,
        regime_breakdown_present=True,
        stress_test_passed=True,
        beats_random_policy=True,
        beats_previous_champion=True,
        tests_passing=True,
        account_telemetry_valid=True,
        real_money_locked=True,
    )


def _artifact(
    store: ImmutableArtifactStore,
    tmp_path: Path,
    candidate_id: str,
    payload: bytes,
) -> str:
    source = tmp_path / f"source-{candidate_id}"
    source.mkdir()
    model = source / "model.bin"
    model.write_bytes(payload)
    return store.ingest(
        candidate_id=candidate_id,
        source_dir=source,
        provenance=_manifest(candidate_id),
        validation=_validation(candidate_id),
        files=[model],
    )


def _snapshot(payload: dict | None = None) -> ShadowMarketSnapshot:
    return ShadowMarketSnapshot.capture(
        payload or {"close": 4337.5, "spread": 0.3, "features": [1.0, 2.0]},
        symbol="XAUUSDm",
        timeframe="M1",
        market_timestamp="2026-08-08T12:00:00+00:00",
        captured_at="2026-08-08T12:00:05+00:00",
    )


def _service(
    tmp_path: Path,
    champion_eval,
    challenger_eval,
):
    store = ImmutableArtifactStore(tmp_path / "objects")
    champion_artifact = _artifact(store, tmp_path, "champion-001", b"champion")
    challenger_artifact = _artifact(store, tmp_path, "challenger-001", b"challenger")
    service = ShadowChallengerService(
        artifact_store=store,
        champion=ShadowParticipant("champion-001", champion_artifact, champion_eval),
        challenger=ShadowParticipant("challenger-001", challenger_artifact, challenger_eval),
        audit_path=tmp_path / "audit" / "shadow.jsonl",
    )
    return service, store, champion_artifact, challenger_artifact


def test_both_models_receive_identical_independent_snapshot_copies(tmp_path: Path):
    observed: list[tuple[str, dict, int]] = []

    def champion(payload):
        observed.append(("champion", dict(payload), id(payload)))
        return {"action": "BUY", "score": 0.8, "reason": "trend"}

    def challenger(payload):
        observed.append(("challenger", dict(payload), id(payload)))
        return {"action": "BUY", "score": 0.7, "reason": "trend"}

    service, *_ = _service(tmp_path, champion, challenger)
    comparison = service.evaluate(_snapshot())

    assert comparison.alignment == "AGREE"
    assert comparison.champion.action == "BUY"
    assert comparison.challenger.action == "BUY"
    assert comparison.champion.trace_id == comparison.challenger.trace_id
    assert comparison.champion.snapshot_id == comparison.challenger.snapshot_id
    assert observed[0][1] == observed[1][1]
    assert observed[0][2] != observed[1][2], "each evaluator gets an independent copy"
    assert comparison.shadow_only is True
    assert comparison.execution_authority_granted is False


def test_opposite_non_wait_actions_are_recorded_as_conflict(tmp_path: Path):
    service, *_ = _service(
        tmp_path,
        lambda _payload: {"action": "BUY"},
        lambda _payload: {"action": "SELL"},
    )
    comparison = service.evaluate(_snapshot())
    assert comparison.alignment == "CONFLICT"
    assert comparison.champion.action == "BUY"
    assert comparison.challenger.action == "SELL"


def test_evaluator_exception_fails_closed_to_wait(tmp_path: Path):
    def broken(_payload):
        raise RuntimeError("model unavailable")

    service, *_ = _service(
        tmp_path,
        broken,
        lambda _payload: {"action": "BUY"},
    )
    comparison = service.evaluate(_snapshot())
    assert comparison.alignment == "ERROR"
    assert comparison.champion.action == "WAIT"
    assert "evaluator_error:RuntimeError" in (comparison.champion.error or "")
    assert comparison.challenger.action == "BUY"
    assert comparison.champion.execution_authority_granted is False


def test_mutating_snapshot_is_rejected_and_cannot_affect_other_model(tmp_path: Path):
    challenger_seen = {}

    def mutating(payload):
        payload["close"] = 999999.0
        return {"action": "BUY"}

    def challenger(payload):
        challenger_seen.update(payload)
        return {"action": "WAIT"}

    service, *_ = _service(tmp_path, mutating, challenger)
    comparison = service.evaluate(_snapshot())
    assert comparison.champion.action == "WAIT"
    assert comparison.champion.error == "evaluator_mutated_snapshot"
    assert challenger_seen["close"] == 4337.5
    assert comparison.alignment == "ERROR"


def test_invalid_action_fails_closed_to_wait(tmp_path: Path):
    service, *_ = _service(
        tmp_path,
        lambda _payload: {"action": "OPEN_MAX_LEVERAGE"},
        lambda _payload: {"action": "WAIT"},
    )
    comparison = service.evaluate(_snapshot())
    assert comparison.champion.action == "WAIT"
    assert comparison.champion.error == "invalid_action:OPEN_MAX_LEVERAGE"
    assert comparison.alignment == "ERROR"


def test_tampered_artifact_blocks_both_evaluators(tmp_path: Path):
    calls: list[str] = []

    def champion(_payload):
        calls.append("champion")
        return {"action": "BUY"}

    def challenger(_payload):
        calls.append("challenger")
        return {"action": "SELL"}

    service, store, champion_artifact, _ = _service(tmp_path, champion, challenger)
    model = store.objects / champion_artifact / "payload" / "model.bin"
    model.write_bytes(b"tampered")

    comparison = service.evaluate(_snapshot())
    assert calls == [], "no model evaluates when either artifact fails integrity"
    assert comparison.champion.action == "WAIT"
    assert comparison.challenger.action == "WAIT"
    assert comparison.alignment == "ERROR"
    assert "champion_artifact_invalid" in (comparison.champion.error or "")


def test_snapshot_is_immutable_after_capture():
    raw = {"close": 100.0, "nested": {"value": 1}}
    snapshot = _snapshot(raw)
    raw["close"] = 200.0
    raw["nested"]["value"] = 99
    assert snapshot.payload()["close"] == 100.0
    assert snapshot.payload()["nested"]["value"] == 1


def test_audit_is_append_only_and_excludes_raw_market_payload(tmp_path: Path):
    service, *_ = _service(
        tmp_path,
        lambda _payload: {"action": "WAIT"},
        lambda _payload: {"action": "WAIT"},
    )
    first = service.evaluate(_snapshot({"secret_market_field": "not-in-audit"}))
    second = service.evaluate(_snapshot({"secret_market_field": "second"}))

    lines = service.audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert rows[0]["trace_id"] == first.trace_id
    assert rows[1]["trace_id"] == second.trace_id
    assert rows[0]["trace_id"] != rows[1]["trace_id"]
    assert "payload" not in rows[0]["snapshot"]
    assert "secret_market_field" not in lines[0]
    assert all(row["shadow_only"] is True for row in rows)
    assert all(row["execution_authority_granted"] is False for row in rows)


def test_result_contains_no_executable_intent_or_order(tmp_path: Path):
    service, *_ = _service(
        tmp_path,
        lambda _payload: {"action": "BUY", "evidence": {"edge": 0.2}},
        lambda _payload: {"action": "SELL", "evidence": {"edge": -0.1}},
    )
    payload = service.evaluate(_snapshot()).to_dict(include_payload=True)
    encoded = json.dumps(payload).lower()
    assert "order_send" not in encoded
    assert "trade_action_deal" not in encoded
    assert "execution_authority_granted\": true" not in encoded
    assert "intent_queue" not in encoded


def test_participant_identity_must_be_unique(tmp_path: Path):
    store = ImmutableArtifactStore(tmp_path / "objects")
    artifact = _artifact(store, tmp_path, "same", b"same")
    participant = ShadowParticipant("same", artifact, lambda _payload: {"action": "WAIT"})
    with pytest.raises(ValueError, match="must differ"):
        ShadowChallengerService(
            artifact_store=store,
            champion=participant,
            challenger=participant,
            audit_path=tmp_path / "audit.jsonl",
        )
