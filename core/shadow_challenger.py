"""Shadow-only champion/challenger comparison service.

Both participants receive independent copies of the exact same immutable market
snapshot.  Their decisions are measured and recorded, but never routed into
risk sizing, verification, queues, broker adapters, dashboard controls, or MT5.

Fail-closed rules:

* both immutable artifacts must verify before either evaluator runs;
* evaluator exceptions, invalid actions or input mutation become ``WAIT``;
* every persisted record forces ``shadow_only=true`` and
  ``execution_authority_granted=false``;
* there is intentionally no method that returns an executable intent.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from core.model_artifacts import ImmutableArtifactStore
from core.model_governance import fingerprint_payload


SHADOW_DECISION_SCHEMA_VERSION = 1
_ALLOWED_ACTIONS = frozenset({"BUY", "SELL", "WAIT"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


@dataclass(frozen=True)
class ShadowMarketSnapshot:
    snapshot_id: str
    payload_hash: str
    payload_json: str
    captured_at: str
    symbol: str = ""
    timeframe: str = ""
    market_timestamp: str = ""
    schema_version: int = SHADOW_DECISION_SCHEMA_VERSION

    @classmethod
    def capture(
        cls,
        payload: Mapping[str, Any],
        *,
        symbol: str = "",
        timeframe: str = "",
        market_timestamp: str = "",
        captured_at: str | None = None,
    ) -> "ShadowMarketSnapshot":
        if not isinstance(payload, Mapping):
            raise TypeError("snapshot payload must be a mapping")
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        payload_hash = fingerprint_payload(json.loads(canonical))
        context = {
            "payload_hash": payload_hash,
            "symbol": str(symbol),
            "timeframe": str(timeframe),
            "market_timestamp": str(market_timestamp),
        }
        return cls(
            snapshot_id=fingerprint_payload(context),
            payload_hash=payload_hash,
            payload_json=canonical,
            captured_at=captured_at or _utc_now(),
            symbol=str(symbol),
            timeframe=str(timeframe),
            market_timestamp=str(market_timestamp),
        )

    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise PermissionError("immutable market snapshot payload is not an object")
        if fingerprint_payload(value) != self.payload_hash:
            raise PermissionError("immutable market snapshot integrity failed")
        return value

    def to_dict(self, *, include_payload: bool = False) -> dict[str, Any]:
        output = {
            "snapshot_id": self.snapshot_id,
            "payload_hash": self.payload_hash,
            "captured_at": self.captured_at,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "market_timestamp": self.market_timestamp,
            "schema_version": self.schema_version,
        }
        if include_payload:
            output["payload"] = self.payload()
        return output


@dataclass(frozen=True)
class ShadowParticipant:
    participant_id: str
    artifact_id: str
    evaluator: Callable[[Mapping[str, Any]], Mapping[str, Any]] = field(
        repr=False, compare=False
    )

    def validate(self) -> None:
        if not str(self.participant_id or "").strip():
            raise ValueError("participant_id is required")
        if not str(self.artifact_id or "").strip():
            raise ValueError("artifact_id is required")
        if not callable(self.evaluator):
            raise TypeError("participant evaluator must be callable")


@dataclass(frozen=True)
class ShadowDecision:
    trace_id: str
    decision_id: str
    snapshot_id: str
    participant_id: str
    artifact_id: str
    role: str
    action: str
    score: float | None
    reason: str
    latency_ms: float
    error: str | None
    evidence: Mapping[str, Any]
    evaluated_at: str = field(default_factory=_utc_now)
    schema_version: int = SHADOW_DECISION_SCHEMA_VERSION
    shadow_only: bool = True
    execution_authority_granted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShadowComparison:
    trace_id: str
    snapshot: ShadowMarketSnapshot
    champion: ShadowDecision
    challenger: ShadowDecision
    alignment: str
    evaluated_at: str = field(default_factory=_utc_now)
    schema_version: int = SHADOW_DECISION_SCHEMA_VERSION
    shadow_only: bool = True
    execution_authority_granted: bool = False

    def to_dict(self, *, include_payload: bool = False) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "snapshot": self.snapshot.to_dict(include_payload=include_payload),
            "champion": self.champion.to_dict(),
            "challenger": self.challenger.to_dict(),
            "alignment": self.alignment,
            "evaluated_at": self.evaluated_at,
            "schema_version": self.schema_version,
            "shadow_only": True,
            "execution_authority_granted": False,
        }


class ShadowChallengerService:
    """Evaluate verified champion/challenger artifacts on identical snapshots."""

    _audit_lock = threading.RLock()

    def __init__(
        self,
        *,
        artifact_store: ImmutableArtifactStore,
        champion: ShadowParticipant,
        challenger: ShadowParticipant,
        audit_path: str | Path,
    ) -> None:
        champion.validate()
        challenger.validate()
        if champion.participant_id == challenger.participant_id:
            raise ValueError("champion and challenger participant IDs must differ")
        self.artifact_store = artifact_store
        self.champion = champion
        self.challenger = challenger
        self.audit_path = Path(audit_path)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _alignment(champion: ShadowDecision, challenger: ShadowDecision) -> str:
        if champion.error or challenger.error:
            return "ERROR"
        if champion.action == "WAIT" and challenger.action == "WAIT":
            return "BOTH_WAIT"
        if champion.action == challenger.action:
            return "AGREE"
        if champion.action == "WAIT":
            return "CHAMPION_WAIT"
        if challenger.action == "WAIT":
            return "CHALLENGER_WAIT"
        return "CONFLICT"

    @staticmethod
    def _decision_id(trace_id: str, participant: ShadowParticipant, role: str) -> str:
        return fingerprint_payload(
            {
                "trace_id": trace_id,
                "participant_id": participant.participant_id,
                "artifact_id": participant.artifact_id,
                "role": role,
            }
        )

    def _wait_decision(
        self,
        *,
        trace_id: str,
        snapshot: ShadowMarketSnapshot,
        participant: ShadowParticipant,
        role: str,
        error: str,
        latency_ms: float = 0.0,
    ) -> ShadowDecision:
        return ShadowDecision(
            trace_id=trace_id,
            decision_id=self._decision_id(trace_id, participant, role),
            snapshot_id=snapshot.snapshot_id,
            participant_id=participant.participant_id,
            artifact_id=participant.artifact_id,
            role=role,
            action="WAIT",
            score=None,
            reason="Shadow evaluation unavailable",
            latency_ms=round(float(latency_ms), 3),
            error=str(error),
            evidence={},
        )

    def _evaluate_participant(
        self,
        *,
        trace_id: str,
        snapshot: ShadowMarketSnapshot,
        participant: ShadowParticipant,
        role: str,
    ) -> ShadowDecision:
        payload = snapshot.payload()
        before_hash = fingerprint_payload(payload)
        started = time.perf_counter()
        try:
            raw = participant.evaluator(payload)
            latency_ms = (time.perf_counter() - started) * 1000.0
            if fingerprint_payload(payload) != before_hash:
                return self._wait_decision(
                    trace_id=trace_id,
                    snapshot=snapshot,
                    participant=participant,
                    role=role,
                    error="evaluator_mutated_snapshot",
                    latency_ms=latency_ms,
                )
            if not isinstance(raw, Mapping):
                return self._wait_decision(
                    trace_id=trace_id,
                    snapshot=snapshot,
                    participant=participant,
                    role=role,
                    error="evaluator_result_not_mapping",
                    latency_ms=latency_ms,
                )
            result = _json_copy(dict(raw))
            action = str(result.get("action") or "WAIT").upper()
            if action not in _ALLOWED_ACTIONS:
                return self._wait_decision(
                    trace_id=trace_id,
                    snapshot=snapshot,
                    participant=participant,
                    role=role,
                    error=f"invalid_action:{action}",
                    latency_ms=latency_ms,
                )
            score_raw = result.get("score")
            score: float | None = None
            if score_raw is not None:
                try:
                    candidate_score = float(score_raw)
                    if math.isfinite(candidate_score):
                        score = candidate_score
                except (TypeError, ValueError):
                    score = None
            reason = str(result.get("reason") or "")
            evidence_raw = result.get("evidence")
            evidence = evidence_raw if isinstance(evidence_raw, Mapping) else {}
            return ShadowDecision(
                trace_id=trace_id,
                decision_id=self._decision_id(trace_id, participant, role),
                snapshot_id=snapshot.snapshot_id,
                participant_id=participant.participant_id,
                artifact_id=participant.artifact_id,
                role=role,
                action=action,
                score=score,
                reason=reason,
                latency_ms=round(latency_ms, 3),
                error=None,
                evidence=_json_copy(dict(evidence)),
            )
        except Exception as exc:  # noqa: BLE001 - shadow failures must be recorded
            latency_ms = (time.perf_counter() - started) * 1000.0
            return self._wait_decision(
                trace_id=trace_id,
                snapshot=snapshot,
                participant=participant,
                role=role,
                error=f"evaluator_error:{type(exc).__name__}:{exc}",
                latency_ms=latency_ms,
            )

    def _append_audit(self, comparison: ShadowComparison) -> None:
        row = json.dumps(
            comparison.to_dict(include_payload=False),
            sort_keys=True,
            default=str,
        ) + "\n"
        with self._audit_lock:
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(row)
                handle.flush()
                os.fsync(handle.fileno())

    def evaluate(self, snapshot: ShadowMarketSnapshot) -> ShadowComparison:
        if not isinstance(snapshot, ShadowMarketSnapshot):
            raise TypeError("evaluate expects ShadowMarketSnapshot")
        # Verify snapshot integrity before touching either evaluator.
        snapshot.payload()
        trace_id = uuid.uuid4().hex

        verification_errors: list[str] = []
        for role, participant in (
            ("champion", self.champion),
            ("challenger", self.challenger),
        ):
            verification = self.artifact_store.verify(participant.artifact_id)
            if not verification.ok:
                verification_errors.append(
                    f"{role}_artifact_invalid:" + ",".join(verification.failures)
                )

        if verification_errors:
            error = ";".join(verification_errors)
            champion_decision = self._wait_decision(
                trace_id=trace_id,
                snapshot=snapshot,
                participant=self.champion,
                role="champion",
                error=error,
            )
            challenger_decision = self._wait_decision(
                trace_id=trace_id,
                snapshot=snapshot,
                participant=self.challenger,
                role="challenger",
                error=error,
            )
        else:
            champion_decision = self._evaluate_participant(
                trace_id=trace_id,
                snapshot=snapshot,
                participant=self.champion,
                role="champion",
            )
            challenger_decision = self._evaluate_participant(
                trace_id=trace_id,
                snapshot=snapshot,
                participant=self.challenger,
                role="challenger",
            )

        comparison = ShadowComparison(
            trace_id=trace_id,
            snapshot=snapshot,
            champion=champion_decision,
            challenger=challenger_decision,
            alignment=self._alignment(champion_decision, challenger_decision),
        )
        self._append_audit(comparison)
        return comparison


__all__ = [
    "SHADOW_DECISION_SCHEMA_VERSION",
    "ShadowChallengerService",
    "ShadowComparison",
    "ShadowDecision",
    "ShadowMarketSnapshot",
    "ShadowParticipant",
]
