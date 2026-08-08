"""Immutable, shadow-only model artifact storage for Quant OS.

This module is intentionally broker-agnostic.  It does not import MT5,
execution loops, order routers, profile mutation, or dashboard controls.

The design harvests the useful model-integrity ideas from the older research
stacks while tightening them for Quant OS:

* artifacts are copied into a content-addressed, immutable object directory;
* provenance and validation evidence are bound into the artifact identity;
* every file is SHA-256 verified before shadow staging;
* every file is SHA-256 verified again before a caller may load it;
* shadow-canary stage/rollback actions are written to an append-only audit log;
* no API in this module can grant execution authority.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from core.model_governance import (
    PromotionDecision,
    ProvenanceManifest,
    ShadowModelRegistry,
    ValidationArtifact,
    fingerprint_payload,
    sha256_file,
)


ARTIFACT_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_relative_path(value: str | Path) -> str:
    """Return a canonical POSIX relative path or raise on traversal/absolute paths."""
    text = str(value).replace("\\", "/")
    rel = PurePosixPath(text)
    if rel.is_absolute() or not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
        raise ValueError(f"unsafe artifact relative path: {value}")
    return rel.as_posix()


@dataclass(frozen=True)
class ArtifactVerification:
    artifact_id: str
    ok: bool
    failures: tuple[str, ...]
    verified_at: str = field(default_factory=_utc_now)
    shadow_only: bool = True
    execution_authority_granted: bool = False

    def require_ok(self) -> "ArtifactVerification":
        if not self.ok:
            raise PermissionError(
                "artifact integrity verification failed: " + ",".join(self.failures)
            )
        return self


class ImmutableArtifactStore:
    """Content-addressed immutable object store for research model bundles."""

    _lock = threading.RLock()

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)

    def _descriptor(
        self,
        *,
        candidate_id: str,
        provenance: ProvenanceManifest,
        validation: ValidationArtifact,
        files: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "provenance_hash": fingerprint_payload(asdict(provenance)),
            "validation_hash": fingerprint_payload(validation.to_dict()),
            "files": {key: dict(value) for key, value in sorted(files.items())},
            "shadow_only": True,
            "execution_authority_granted": False,
        }

    def ingest(
        self,
        *,
        candidate_id: str,
        source_dir: str | Path,
        provenance: ProvenanceManifest,
        validation: ValidationArtifact,
        files: Sequence[str | Path],
    ) -> str:
        """Copy a candidate bundle into the immutable object store.

        The resulting artifact ID binds file hashes, provenance, validation
        evidence, and candidate identity.  Existing identical objects are
        reused only after they re-verify successfully.
        """
        if not candidate_id or candidate_id != provenance.candidate_id:
            raise ValueError("candidate/provenance candidate_id mismatch")
        if candidate_id != validation.candidate_id:
            raise ValueError("candidate/validation candidate_id mismatch")
        provenance_failures = provenance.validate()
        if provenance_failures:
            raise ValueError("invalid provenance: " + ",".join(provenance_failures))
        if not files:
            raise ValueError("artifact must contain at least one file")

        source_root = Path(source_dir).resolve()
        if not source_root.is_dir():
            raise NotADirectoryError(source_root)

        file_meta: dict[str, dict[str, Any]] = {}
        source_paths: dict[str, Path] = {}
        for raw in files:
            path = Path(raw).resolve()
            try:
                relative = path.relative_to(source_root)
            except ValueError as exc:
                raise ValueError(f"artifact outside source_dir: {path}") from exc
            rel = _safe_relative_path(relative)
            if rel in file_meta:
                raise ValueError(f"duplicate artifact path: {rel}")
            if not path.is_file():
                raise FileNotFoundError(path)
            file_meta[rel] = {
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
            source_paths[rel] = path

        descriptor = self._descriptor(
            candidate_id=candidate_id,
            provenance=provenance,
            validation=validation,
            files=file_meta,
        )
        artifact_id = fingerprint_payload(descriptor)
        final_dir = self.objects / artifact_id

        with self._lock:
            if final_dir.exists():
                self.verify(artifact_id).require_ok()
                return artifact_id

            tmp_dir = Path(tempfile.mkdtemp(prefix="artifact-", dir=str(self.objects)))
            try:
                payload_dir = tmp_dir / "payload"
                payload_dir.mkdir(parents=True, exist_ok=True)
                for rel, src in source_paths.items():
                    dst = payload_dir / Path(rel)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    with dst.open("rb") as handle:
                        os.fsync(handle.fileno())

                manifest = {
                    **descriptor,
                    "artifact_id": artifact_id,
                    "created_at": _utc_now(),
                }
                manifest_path = tmp_dir / "manifest.json"
                with manifest_path.open("w", encoding="utf-8") as handle:
                    json.dump(manifest, handle, indent=2, sort_keys=True, default=str)
                    handle.flush()
                    os.fsync(handle.fileno())

                # Verify the temporary object before publication.
                self._verify_dir(tmp_dir, expected_artifact_id=artifact_id).require_ok()
                try:
                    os.replace(tmp_dir, final_dir)
                except FileExistsError:
                    # Another writer published the exact same content-addressed
                    # object first.  Never overwrite it; verify and reuse it.
                    self.verify(artifact_id).require_ok()
                return artifact_id
            finally:
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir, ignore_errors=True)

    def manifest(self, artifact_id: str) -> dict[str, Any]:
        path = self.objects / artifact_id / "manifest.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PermissionError(f"artifact manifest is corrupt: {artifact_id}") from exc
        if not isinstance(payload, dict):
            raise PermissionError(f"artifact manifest is invalid: {artifact_id}")
        return payload

    def _verify_dir(self, object_dir: Path, *, expected_artifact_id: str) -> ArtifactVerification:
        failures: list[str] = []
        manifest_path = object_dir / "manifest.json"
        if not manifest_path.is_file():
            return ArtifactVerification(expected_artifact_id, False, ("manifest_missing",))
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ArtifactVerification(expected_artifact_id, False, ("manifest_unreadable",))

        if manifest.get("artifact_id") != expected_artifact_id:
            failures.append("artifact_id_mismatch")
        if manifest.get("shadow_only") is not True:
            failures.append("shadow_only_not_true")
        if manifest.get("execution_authority_granted") is not False:
            failures.append("unsafe_execution_authority")

        descriptor = {
            "schema_version": manifest.get("schema_version"),
            "candidate_id": manifest.get("candidate_id"),
            "provenance_hash": manifest.get("provenance_hash"),
            "validation_hash": manifest.get("validation_hash"),
            "files": manifest.get("files") or {},
            "shadow_only": manifest.get("shadow_only"),
            "execution_authority_granted": manifest.get("execution_authority_granted"),
        }
        if fingerprint_payload(descriptor) != expected_artifact_id:
            failures.append("descriptor_hash_mismatch")

        file_meta = manifest.get("files")
        if not isinstance(file_meta, dict) or not file_meta:
            failures.append("files_missing")
            file_meta = {}

        payload_root = object_dir / "payload"
        for raw_rel, raw_meta in file_meta.items():
            try:
                rel = _safe_relative_path(raw_rel)
            except ValueError:
                failures.append(f"unsafe_path:{raw_rel}")
                continue
            meta = raw_meta if isinstance(raw_meta, dict) else {}
            file_path = payload_root / Path(rel)
            if not file_path.is_file():
                failures.append(f"missing_file:{rel}")
                continue
            expected_size = meta.get("size")
            if expected_size is not None and file_path.stat().st_size != int(expected_size):
                failures.append(f"size_mismatch:{rel}")
            expected_hash = str(meta.get("sha256") or "")
            if not expected_hash or sha256_file(file_path) != expected_hash:
                failures.append(f"sha256_mismatch:{rel}")

        unique = tuple(dict.fromkeys(failures))
        return ArtifactVerification(expected_artifact_id, not unique, unique)

    def verify(self, artifact_id: str) -> ArtifactVerification:
        object_dir = self.objects / artifact_id
        if not object_dir.is_dir():
            return ArtifactVerification(artifact_id, False, ("artifact_missing",))
        return self._verify_dir(object_dir, expected_artifact_id=artifact_id)

    def require_verified(self, artifact_id: str) -> ArtifactVerification:
        return self.verify(artifact_id).require_ok()

    def prepare_load(self, artifact_id: str, relative_path: str | Path) -> Path:
        """Re-verify the whole object immediately before returning a model path."""
        self.require_verified(artifact_id)
        rel = _safe_relative_path(relative_path)
        manifest = self.manifest(artifact_id)
        if rel not in (manifest.get("files") or {}):
            raise FileNotFoundError(f"file not declared in artifact manifest: {rel}")
        path = self.objects / artifact_id / "payload" / Path(rel)
        if not path.is_file():
            raise FileNotFoundError(path)
        # Verify the exact requested file again after resolving it.
        expected_hash = str(manifest["files"][rel].get("sha256") or "")
        if not expected_hash or sha256_file(path) != expected_hash:
            raise PermissionError(f"artifact file integrity failed before load: {rel}")
        return path


class ShadowArtifactGate:
    """Integrity gate between immutable artifacts and ShadowModelRegistry.

    A successful call can only stage a shadow canary.  Rollback means selecting
    an earlier *shadow* candidate again.  Neither action can grant broker or
    execution authority.
    """

    _audit_lock = threading.RLock()

    def __init__(self, store: ImmutableArtifactStore, audit_path: str | Path | None = None) -> None:
        self.store = store
        self.audit_path = Path(audit_path) if audit_path else store.root / "shadow_lifecycle.jsonl"
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    def _candidate_for_artifact(self, artifact_id: str) -> str:
        manifest = self.store.manifest(artifact_id)
        candidate_id = str(manifest.get("candidate_id") or "")
        if not candidate_id:
            raise PermissionError("artifact has no candidate identity")
        return candidate_id

    def _append_audit(self, payload: Mapping[str, Any]) -> None:
        row = {
            **dict(payload),
            "timestamp": _utc_now(),
            "shadow_only": True,
            "execution_authority_granted": False,
        }
        encoded = json.dumps(row, sort_keys=True, default=str) + "\n"
        with self._audit_lock:
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())

    def stage(
        self,
        *,
        registry: ShadowModelRegistry,
        candidate_id: str,
        artifact_id: str,
        decision: PromotionDecision,
        action: str = "stage",
    ) -> dict[str, Any]:
        if action not in {"stage", "rollback"}:
            raise ValueError(f"unsupported shadow lifecycle action: {action}")
        self.store.require_verified(artifact_id)
        artifact_candidate = self._candidate_for_artifact(artifact_id)
        if artifact_candidate != candidate_id:
            raise PermissionError("artifact/candidate identity mismatch")
        if decision.candidate_id != candidate_id:
            raise PermissionError("decision/candidate identity mismatch")
        if decision.execution_authority_granted or not decision.shadow_only:
            raise PermissionError("unsafe promotion decision")

        before = registry.state()
        previous = before.get("shadow_canary")
        state = registry.stage_shadow_canary(candidate_id, decision)
        self._append_audit(
            {
                "action": action,
                "candidate_id": candidate_id,
                "artifact_id": artifact_id,
                "previous_shadow_canary": previous,
                "current_shadow_canary": state.get("shadow_canary"),
            }
        )
        return state

    def rollback(
        self,
        *,
        registry: ShadowModelRegistry,
        candidate_id: str,
        artifact_id: str,
        decision: PromotionDecision,
    ) -> dict[str, Any]:
        """Return to a previously registered, still-verified shadow candidate."""
        return self.stage(
            registry=registry,
            candidate_id=candidate_id,
            artifact_id=artifact_id,
            decision=decision,
            action="rollback",
        )

    def prepare_load(self, *, candidate_id: str, artifact_id: str, relative_path: str | Path) -> Path:
        """Fail closed before a shadow evaluator opens a model artifact."""
        artifact_candidate = self._candidate_for_artifact(artifact_id)
        if artifact_candidate != candidate_id:
            raise PermissionError("artifact/candidate identity mismatch before load")
        return self.store.prepare_load(artifact_id, relative_path)


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ArtifactVerification",
    "ImmutableArtifactStore",
    "ShadowArtifactGate",
]
