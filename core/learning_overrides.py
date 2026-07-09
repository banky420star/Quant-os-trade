"""Learning config overrides read-back (Phase 2.4 fix).

When the learning loop is in `live_apply_limited` mode it writes bounded,
safety-checked patches to state/learning_config_overrides.json. This module
reads those patches back and applies them to a config copy so the live decision
path (signal/evaluation/verifier/fast) actually honours them. It never edits
config.yaml. Rollback removes a patch when post-apply performance degrades.
"""

from __future__ import annotations

from typing import Any

from core.config_proposal import apply_patch_to_config, is_dangerous
from core.utils import read_json_state, utc_now_iso, write_json_state

OVERRIDES_FILE = "learning_config_overrides.json"


def read_overrides() -> dict[str, Any]:
    return read_json_state(OVERRIDES_FILE, default={"patches": []}) or {"patches": []}


def active_patches() -> list[dict[str, Any]]:
    return list(read_overrides().get("patches") or [])


def apply_learning_overrides(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return (config_with_overrides, applied_proposal_ids). No-op if no patches.

    Re-checks the safety policy at read-back time so a corrupted/tampered patch
    file can never bypass the guard.
    """
    patches = active_patches()
    if not patches:
        return config, []
    merged = config
    applied: list[str] = []
    for entry in patches:
        patch = entry.get("patch") or {}
        reviewed = int((entry.get("evidence") or {}).get("reviewed_trades") or 0)
        dangerous, why = is_dangerous(patch, reviewed_trades=reviewed)
        if dangerous:
            # a patch that became dangerous (e.g. config drifted) is dropped
            rollback_patch(entry.get("proposal_id"), f"unsafe_at_readback:{why}")
            continue
        merged = apply_patch_to_config(merged, patch)
        applied.append(entry.get("proposal_id"))
    return merged, applied


def rollback_patch(proposal_id: str | None, reason: str) -> None:
    """Remove a patch from the override file and record a rollback marker."""
    if not proposal_id:
        return
    doc = read_overrides()
    remaining = [p for p in (doc.get("patches") or []) if p.get("proposal_id") != proposal_id]
    doc["patches"] = remaining
    doc.setdefault("rollbacks", []).append({
        "proposal_id": proposal_id, "reason": reason, "rolled_back_at": utc_now_iso(),
    })
    doc["updated_at"] = utc_now_iso()
    write_json_state(OVERRIDES_FILE, doc)


def record_patch_baseline(proposal_id: str, baseline_expectancy: float) -> None:
    """Stamp an applied patch with the rolling expectancy at apply time."""
    doc = read_overrides()
    for p in doc.get("patches") or []:
        if p.get("proposal_id") == proposal_id:
            p["baseline_expectancy_r"] = baseline_expectancy
    doc["updated_at"] = utc_now_iso()
    write_json_state(OVERRIDES_FILE, doc)
