"""Shadow-only model governance for Quant OS.

Harvests the strongest ideas from the research repositories without importing
legacy execution paths:

* content-addressed feature/dataset/model provenance;
* standardized validation artifacts;
* deterministic, explainable promotion gates;
* a file-backed challenger/canary registry with artifact integrity checks;
* explicit prohibition on live execution authority.

This module is deliberately broker-agnostic and MUST NOT import MT5, execution
loops, order routers, or dashboard mutation code.  A passing promotion decision
means "eligible for shadow canary evaluation" only.  It never enables trading.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


GOVERNANCE_SCHEMA_VERSION = 1
SHADOW_ONLY = True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_payload(value: Any) -> str:
    """Stable SHA-256 fingerprint for JSON-serializable research inputs."""
    return sha256_bytes(_canonical_json(value))


def fingerprint_feature_matrix(matrix: Any) -> str:
    """Fingerprint a feature matrix without requiring numpy.

    Objects exposing ``tobytes`` (numpy/pandas-backed arrays) are hashed from
    their raw bytes plus shape/dtype metadata.  Plain nested sequences fall
    back to canonical JSON.  The goal is to catch silent ablation failures:
    if a feature-removal experiment claims to change the matrix, its
    fingerprint must differ from the control.
    """
    raw = getattr(matrix, "tobytes", None)
    if callable(raw):
        metadata = {
            "shape": list(getattr(matrix, "shape", ()) or ()),
            "dtype": str(getattr(matrix, "dtype", "unknown")),
        }
        return sha256_bytes(_canonical_json(metadata) + raw())
    return fingerprint_payload(matrix)


@dataclass(frozen=True)
class ProvenanceManifest:
    """Immutable provenance attached to every research candidate."""

    candidate_id: str
    git_commit: str
    dataset_id: str
    dataset_sha256: str
    feature_set_id: str
    feature_fingerprint: str
    random_seed: int
    created_at: str = field(default_factory=_utc_now)
    schema_version: int = GOVERNANCE_SCHEMA_VERSION
    notes: str = ""

    def validate(self) -> list[str]:
        failures: list[str] = []
        required = {
            "candidate_id": self.candidate_id,
            "git_commit": self.git_commit,
            "dataset_id": self.dataset_id,
            "dataset_sha256": self.dataset_sha256,
            "feature_set_id": self.feature_set_id,
            "feature_fingerprint": self.feature_fingerprint,
        }
        for key, value in required.items():
            if not str(value or "").strip():
                failures.append(f"missing_{key}")
        for key, value in (
            ("dataset_sha256", self.dataset_sha256),
            ("feature_fingerprint", self.feature_fingerprint),
        ):
            text = str(value or "")
            if text and (len(text) != 64 or any(c not in "0123456789abcdef" for c in text.lower())):
                failures.append(f"invalid_{key}")
        return failures


@dataclass(frozen=True)
class ValidationArtifact:
    """Standard evidence bundle consumed by promotion policy."""

    candidate_id: str
    provenance: ProvenanceManifest
    data_source: str
    has_spread_data: bool
    leakage_detected: bool
    feature_audit_passed: bool
    return_after_costs: float
    profit_factor: float
    sharpe: float
    max_drawdown: float
    trade_count: int
    max_single_trade_profit_share: float
    walk_forward_windows_passed: int
    regime_breakdown_present: bool
    stress_test_passed: bool
    beats_random_policy: bool
    beats_previous_champion: bool
    demo_canary_completed: bool = False
    demo_canary_trades: int = 0
    demo_canary_days: int = 0
    demo_pnl_after_costs: float = 0.0
    tests_passing: bool = False
    account_telemetry_valid: bool = False
    real_money_locked: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)
    schema_version: int = GOVERNANCE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromotionThresholds:
    """Conservative defaults for entering shadow canary evaluation.

    These are not claims of statistical sufficiency for live money.  They are
    deliberately a first governance gate; later canary/live gates should be
    calibrated by strategy frequency and observed uncertainty.
    """

    min_oos_return_after_costs: float = 0.0
    min_profit_factor: float = 1.10
    min_sharpe: float = 0.50
    max_drawdown: float = 0.10
    min_trade_count: int = 100
    max_single_trade_profit_share: float = 0.20
    min_walk_forward_windows_passed: int = 3


@dataclass(frozen=True)
class PromotionDecision:
    candidate_id: str
    eligible_for_shadow_canary: bool
    failures: tuple[str, ...]
    evaluated_at: str = field(default_factory=_utc_now)
    shadow_only: bool = True
    execution_authority_granted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PromotionPolicy:
    """Fail-closed promotion policy.

    A pass means the candidate may be staged for *shadow canary* evaluation.
    This class has no method that grants broker/order authority.
    """

    def __init__(self, thresholds: PromotionThresholds | None = None) -> None:
        self.thresholds = thresholds or PromotionThresholds()

    def evaluate(self, artifact: ValidationArtifact) -> PromotionDecision:
        t = self.thresholds
        failures = list(artifact.provenance.validate())

        if artifact.candidate_id != artifact.provenance.candidate_id:
            failures.append("candidate_id_mismatch")
        if str(artifact.data_source).lower() != "mt5":
            failures.append("data_source_not_mt5")
        if not artifact.has_spread_data:
            failures.append("missing_spread_data")
        if artifact.leakage_detected:
            failures.append("data_leakage_detected")
        if not artifact.feature_audit_passed:
            failures.append("feature_audit_failed")
        if artifact.return_after_costs <= t.min_oos_return_after_costs:
            failures.append("nonpositive_oos_return_after_costs")
        if artifact.profit_factor < t.min_profit_factor:
            failures.append("profit_factor_below_gate")
        if artifact.sharpe < t.min_sharpe:
            failures.append("sharpe_below_gate")
        if artifact.max_drawdown > t.max_drawdown:
            failures.append("drawdown_above_gate")
        if artifact.trade_count < t.min_trade_count:
            failures.append("insufficient_trade_count")
        if artifact.max_single_trade_profit_share > t.max_single_trade_profit_share:
            failures.append("single_trade_concentration")
        if artifact.walk_forward_windows_passed < t.min_walk_forward_windows_passed:
            failures.append("insufficient_walk_forward_windows")
        if not artifact.regime_breakdown_present:
            failures.append("missing_regime_breakdown")
        if not artifact.stress_test_passed:
            failures.append("stress_test_failed")
        if not artifact.beats_random_policy:
            failures.append("fails_random_baseline")
        if not artifact.beats_previous_champion:
            failures.append("fails_previous_champion")
        if not artifact.tests_passing:
            failures.append("tests_not_passing")
        if not artifact.account_telemetry_valid:
            failures.append("account_telemetry_invalid")
        if not artifact.real_money_locked:
            failures.append("real_money_not_locked")

        # Canary evidence is intentionally NOT required to enter canary.  It is
        # required for a future post-canary promotion stage, which is outside
        # this Phase-0-safe module.
        unique_failures = tuple(dict.fromkeys(failures))
        return PromotionDecision(
            candidate_id=artifact.candidate_id,
            eligible_for_shadow_canary=not unique_failures,
            failures=unique_failures,
        )


class ShadowModelRegistry:
    """Integrity-checked research registry with no execution coupling."""

    _lock = threading.RLock()

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.candidates = self.root / "candidates"
        self.candidates.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "active.json"
        if not self.state_path.exists():
            self._write_state(
                {
                    "schema_version": GOVERNANCE_SCHEMA_VERSION,
                    "shadow_only": True,
                    "execution_authority_granted": False,
                    "research_champion": None,
                    "shadow_canary": None,
                    "history": [],
                }
            )

    def _read_state(self) -> dict[str, Any]:
        with self._lock:
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {
                    "schema_version": GOVERNANCE_SCHEMA_VERSION,
                    "shadow_only": True,
                    "execution_authority_granted": False,
                    "research_champion": None,
                    "shadow_canary": None,
                    "history": [],
                }

    def _write_state(self, state: Mapping[str, Any]) -> None:
        payload = dict(state)
        payload["schema_version"] = GOVERNANCE_SCHEMA_VERSION
        payload["shadow_only"] = True
        payload["execution_authority_granted"] = False
        payload["updated_at"] = _utc_now()
        encoded = json.dumps(payload, indent=2, sort_keys=True, default=str)
        with self._lock:
            fd, tmp_name = tempfile.mkstemp(
                prefix="active-", suffix=".tmp", dir=str(self.root), text=True
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self.state_path)
            finally:
                try:
                    if os.path.exists(tmp_name):
                        os.unlink(tmp_name)
                except OSError:
                    pass

    def register_candidate(
        self,
        *,
        artifact_dir: str | Path,
        manifest: ProvenanceManifest,
        validation: ValidationArtifact,
        model_files: Sequence[str | Path],
    ) -> Path:
        if manifest.candidate_id != validation.candidate_id:
            raise ValueError("manifest/validation candidate_id mismatch")
        failures = manifest.validate()
        if failures:
            raise ValueError("invalid provenance: " + ",".join(failures))

        source_root = Path(artifact_dir).resolve()
        record_dir = self.candidates / manifest.candidate_id
        record_dir.mkdir(parents=True, exist_ok=False)

        integrity: dict[str, str] = {}
        for raw in model_files:
            path = Path(raw).resolve()
            try:
                relative = path.relative_to(source_root)
            except ValueError as exc:
                raise ValueError(f"artifact outside artifact_dir: {path}") from exc
            if not path.is_file():
                raise FileNotFoundError(path)
            integrity[str(relative)] = sha256_file(path)

        record = {
            "manifest": asdict(manifest),
            "validation": validation.to_dict(),
            "integrity": integrity,
            "registered_at": _utc_now(),
            "shadow_only": True,
            "execution_authority_granted": False,
        }
        (record_dir / "record.json").write_text(
            json.dumps(record, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        return record_dir

    def stage_shadow_canary(
        self, candidate_id: str, decision: PromotionDecision
    ) -> dict[str, Any]:
        if decision.candidate_id != candidate_id:
            raise ValueError("promotion decision candidate mismatch")
        if not decision.eligible_for_shadow_canary:
            raise PermissionError("candidate failed shadow-canary promotion gates")
        if decision.execution_authority_granted or not decision.shadow_only:
            raise PermissionError("unsafe promotion decision")
        record_path = self.candidates / candidate_id / "record.json"
        if not record_path.exists():
            raise FileNotFoundError(record_path)

        state = self._read_state()
        history = list(state.get("history") or [])
        previous = state.get("shadow_canary")
        if previous and previous != candidate_id:
            history.append(
                {
                    "candidate_id": previous,
                    "role": "shadow_canary",
                    "retired_at": _utc_now(),
                }
            )
        state["shadow_canary"] = candidate_id
        state["history"] = history
        self._write_state(state)
        return self._read_state()

    def state(self) -> dict[str, Any]:
        return self._read_state()


__all__ = [
    "GOVERNANCE_SCHEMA_VERSION",
    "SHADOW_ONLY",
    "ProvenanceManifest",
    "ValidationArtifact",
    "PromotionThresholds",
    "PromotionDecision",
    "PromotionPolicy",
    "ShadowModelRegistry",
    "fingerprint_feature_matrix",
    "fingerprint_payload",
    "sha256_file",
]
