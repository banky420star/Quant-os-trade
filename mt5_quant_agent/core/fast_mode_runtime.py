"""Runtime fast-mode overrides — dashboard/TUI presets without editing YAML."""

from __future__ import annotations

from typing import Any

from core.utils import read_json_state, utc_now_iso, write_json_state

RUNTIME_FILE = "fast_mode_runtime.json"

# Named presets from the HF scalper spec (Modes 0–3).
FAST_MODE_PRESETS: dict[str, dict[str, Any]] = {
    "off": {
        "label": "Off",
        "description": "Disable fast scalper layer entirely",
        "overrides": {"enabled": False, "live_enabled": False},
    },
    "observe": {
        "label": "Observe only",
        "description": "Log would-enter / would-BE decisions — no live orders",
        "overrides": {
            "enabled": True,
            "live_enabled": False,
            "tick_interval_ms": 1000,
            "symbols": ["XAUUSDm", "USOILm", "UK100m"],
            "allow_market_entries": False,
            "allow_limit_entries": True,
        },
    },
    "safe": {
        "label": "Safe scalper",
        "description": "Live limit entries, 1s tick, micro symbols, strict gates",
        "overrides": {
            "enabled": True,
            "live_enabled": True,
            "tick_interval_ms": 1000,
            "symbols": ["XAUUSDm", "USOILm", "UK100m"],
            "allow_market_entries": False,
            "allow_limit_entries": True,
            "max_open_positions": 1,
            "max_trades_per_symbol_per_hour": 4,
            "max_consecutive_losses_per_symbol": 2,
            "require_verifier_approval": True,
        },
    },
    "aggressive": {
        "label": "Aggressive scalper",
        "description": "500ms tick, market entries when spread/zone OK",
        "overrides": {
            "enabled": True,
            "live_enabled": True,
            "tick_interval_ms": 500,
            "symbols": ["XAUUSDm", "USOILm", "UK100m"],
            "allow_market_entries": True,
            "allow_limit_entries": True,
            "market_if_distance_atr_below": 0.05,
            "max_open_positions": 1,
            "max_trades_per_symbol_per_hour": 8,
            "require_verifier_approval": True,
        },
    },
    "sprint": {
        "label": "Sprint (XAU only)",
        "description": "250ms tick, one symbol, one position, emergency exit on",
        "overrides": {
            "enabled": True,
            "live_enabled": True,
            "tick_interval_ms": 250,
            "symbols": ["XAUUSDm"],
            "allow_market_entries": True,
            "allow_limit_entries": True,
            "max_open_positions": 1,
            "max_trades_per_symbol_per_hour": 4,
            "max_trades_per_10min": 2,
            "require_verifier_approval": True,
            "emergency_exit": {"enabled": True},
        },
    },
}


def preset_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": key,
            "label": spec["label"],
            "description": spec["description"],
        }
        for key, spec in FAST_MODE_PRESETS.items()
    ]


def read_runtime() -> dict[str, Any]:
    return read_json_state(RUNTIME_FILE, default={}) or {}


def active_preset_id() -> str | None:
    rt = read_runtime()
    pid = rt.get("preset")
    return str(pid) if pid else None


def merge_fast_mode(base: dict[str, Any]) -> dict[str, Any]:
    """Apply runtime overrides on top of profile/config fast_mode block."""
    merged = dict(base or {})
    rt = read_runtime()
    overrides = dict(rt.get("overrides") or {})
    if overrides:
        merged.update(overrides)
    return merged


def apply_preset(preset_id: str, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist a named preset (and optional field overrides) to state."""
    spec = FAST_MODE_PRESETS.get(preset_id)
    if not spec:
        raise ValueError(f"Unknown fast_mode preset: {preset_id}")
    overrides = dict(spec["overrides"])
    if extra:
        overrides.update(extra)
    doc = {
        "timestamp": utc_now_iso(),
        "preset": preset_id,
        "label": spec["label"],
        "overrides": overrides,
        "source": "fast_mode_runtime",
    }
    write_json_state(RUNTIME_FILE, doc)
    return doc


def apply_overrides(overrides: dict[str, Any], *, preset_id: str | None = None) -> dict[str, Any]:
    """Persist custom overrides (e.g. toggle live_enabled only)."""
    doc = {
        "timestamp": utc_now_iso(),
        "preset": preset_id or "custom",
        "label": FAST_MODE_PRESETS.get(preset_id or "", {}).get("label", "Custom"),
        "overrides": dict(overrides),
        "source": "fast_mode_runtime",
    }
    write_json_state(RUNTIME_FILE, doc)
    return doc


def clear_runtime() -> None:
    """Remove runtime overrides — fall back to profile YAML only."""
    write_json_state(RUNTIME_FILE, {
        "timestamp": utc_now_iso(),
        "preset": None,
        "overrides": {},
        "source": "fast_mode_runtime",
    })