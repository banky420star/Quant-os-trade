"""High-frequency scalping mode — config helpers and symbol scope."""

from __future__ import annotations

from typing import Any

from core.fast_mode_runtime import merge_fast_mode
from core.micro_profile import micro_profile_enabled, micro_settings


def fast_mode_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve effective fast_mode with runtime overrides, gated by profile locks.

    The profile-level fast_mode block is authoritative for 'enabled' and
    'live_enabled'.  A fast_mode_runtime.json preset may OBSERVE a disabled
    profile (collect signals without acting) but may NEVER elevate a profile
    that explicitly set enabled=false or live_enabled=false back to true
    unless the profile also sets the opt-in flags:

        execution:
          allow_fast_mode_runtime_enable: true
          allow_fast_mode_runtime_live: true

    The emergency gate execution.fast_mode_enabled=false still bypasses all
    merging and returns disabled immediately.
    """
    execution = config.get("execution") or {}

    # Hard gate: execution.fast_mode_enabled=False disables everything.
    if execution.get("fast_mode_enabled") is False:
        return {"enabled": False, "live_enabled": False, "symbols": []}

    base = dict(config.get("fast_mode") or {})
    merged = merge_fast_mode(base)

    # Profile-level false is sticky — runtime can observe but not re-enable
    # unless the profile explicitly opts in.
    allow_enable = bool(execution.get("allow_fast_mode_runtime_enable", False))
    allow_live = bool(execution.get("allow_fast_mode_runtime_live", False))

    if base.get("enabled") is False and not allow_enable:
        merged["enabled"] = False
        merged["live_enabled"] = False  # can't be live if not enabled

    if base.get("live_enabled") is False and not allow_live:
        merged["live_enabled"] = False

    return merged


def fast_mode_enabled(config: dict[str, Any]) -> bool:
    return bool(fast_mode_settings(config).get("enabled", False))


def fast_mode_live(config: dict[str, Any]) -> bool:
    """True only when fast layer may place/modify live orders."""
    cfg = fast_mode_settings(config)
    return bool(cfg.get("enabled")) and bool(cfg.get("live_enabled", False))


def fast_mode_observe_only(config: dict[str, Any]) -> bool:
    return fast_mode_enabled(config) and not fast_mode_live(config)


def fast_mode_symbols(config: dict[str, Any]) -> list[str]:
    """Symbols the fast layer may touch — intersected with profile universe."""
    cfg = fast_mode_settings(config)
    fast_syms = list(cfg.get("symbols") or [])
    if micro_profile_enabled(config):
        allowed = set(micro_settings(config).get("symbols") or [])
        if allowed:
            fast_syms = [s for s in fast_syms if s in allowed]
    elif fast_syms:
        mt5_syms = set((config.get("mt5") or {}).get("symbols") or [])
        if mt5_syms:
            fast_syms = [s for s in fast_syms if s in mt5_syms]
    return fast_syms


def tick_interval_seconds(config: dict[str, Any]) -> float:
    ms = int(fast_mode_settings(config).get("tick_interval_ms", 1000))
    return max(0.25, ms / 1000.0)