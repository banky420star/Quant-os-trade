"""High-frequency scalping mode — config helpers and symbol scope."""

from __future__ import annotations

from typing import Any

from core.fast_mode_runtime import merge_fast_mode
from core.micro_profile import micro_profile_enabled, micro_settings


def fast_mode_settings(config: dict[str, Any]) -> dict[str, Any]:
    # Data-lab's fixed-exit experiment has a single order producer. Do not let
    # fast_mode_runtime.json or a global fast-mode preset re-enable the
    # secondary live entry/SL-management service for this profile.
    if bool(config.get("execution", {}).get("fast_mode_enabled", True)) is False:
        return {"enabled": False, "live_enabled": False, "symbols": []}
    return merge_fast_mode(config.get("fast_mode") or {})


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