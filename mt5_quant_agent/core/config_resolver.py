"""Config Resolver — deterministic single source of truth for all config layers.

Problem: Four code paths (sync_practice_gates ×2, sync_micro_profile,
apply_config_overrides) compete to write the same risk/exposure/execution keys.
Order of execution determines the winner, producing non-deterministic behavior
and silent config bugs.

Solution: resolve_effective_config() runs ONCE at startup, merges all layers
in a documented deterministic order, and returns an immutable effective config.
Every loop reads effective_config and NEVER mutates it. The merge log is written
to state/config_resolution.json for debugging.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from core.utils import STATE_DIR, load_config, utc_now_iso


def resolve_effective_config(raw_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Audit the fully-resolved effective config and write a resolution log.

    This function SNAPSHOTS the already-resolved config (which has already been
    through load_config()'s full layer chain) and writes the current state of
    key risk/execution/trading values to state/config_resolution.json for
    debugging. It does NOT re-apply layers — load_config() already did that.

    Call with raw_config=None to load fresh from disk via load_config().
    Call with a pre-loaded config dict to snapshot it directly.

    Returns a deep copy of the config so callers can use it without aliasing.
    """
    if raw_config is None:
        raw_config = load_config()

    # Deep-copy so callers don't accidentally mutate the loaded config.
    config = copy.deepcopy(raw_config)
    change_log: list[dict[str, Any]] = []

    # Snapshot key values for debugging/dashboard consumption.
    risk = config.get("risk", {})
    exec_cfg = config.get("execution", {})
    trading = config.get("trading", {})
    practice = config.get("practice", {})

    final_snapshot = {
        "risk.max_consecutive_losses": risk.get("max_consecutive_losses"),
        "risk.max_total_exposure_usd": risk.get("max_total_exposure_usd"),
        "risk.max_symbol_exposure_usd": risk.get("max_symbol_exposure_usd"),
        "risk.max_drawdown_pct": risk.get("max_drawdown_pct"),
        "risk.max_daily_loss_pct": risk.get("max_daily_loss_pct"),
        "execution.mode": exec_cfg.get("mode"),
        "execution.live_trading_enabled": exec_cfg.get("live_trading_enabled"),
        "execution.starting_cash": exec_cfg.get("starting_cash"),
        "trading.max_open_per_symbol": trading.get("max_open_per_symbol"),
        "practice.micro.enabled": (practice.get("micro") or {}).get("enabled"),
        "practice.growth.enabled": (practice.get("growth") or {}).get("enabled"),
        "practice.relax_structure_checks": practice.get("relax_structure_checks"),
    }

    # Mark as resolved so consumers know this is the effective config.
    config["_resolved"] = True
    config["_resolved_at"] = utc_now_iso()

    # Write the resolution log for dashboard/debugging consumption.
    resolution_doc = {
        "resolved_at": utc_now_iso(),
        "active_profile": config.get("active_profile"),
        "final_snapshot": final_snapshot,
    }
    try:
        (STATE_DIR / "config_resolution.json").write_text(
            json.dumps(resolution_doc, indent=2, default=str), encoding="utf-8"
        )
    except OSError:
        pass

    return config


def effective_config_from_cache() -> dict[str, Any] | None:
    """Return the last resolved effective config if available, or None."""
    try:
        path = STATE_DIR / "config_resolution.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("final_snapshot"):
            return {"_resolved": True, "_from_cache": True, **data}
    except (OSError, json.JSONDecodeError):
        pass
    return None
