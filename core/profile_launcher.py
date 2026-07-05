"""Account-size profile launcher — apply 30 / 100 / growth overlays."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from core.utils import PROJECT_ROOT, read_json_state, write_json_state

PROFILES_DIR = PROJECT_ROOT / "profiles"
STATE_FILE = "active_profile.json"
ENV_VAR = "MT5_QUANT_PROFILE"


def list_profiles() -> list[str]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.yaml"))


def active_profile_name() -> str | None:
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return env
    state = read_json_state(STATE_FILE, default={}) or {}
    name = str(state.get("profile") or "").strip()
    return name or None


def profile_path(name: str) -> Path:
    path = PROFILES_DIR / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Unknown profile '{name}'. Available: {', '.join(list_profiles())}")
    return path


def load_profile_overlay(name: str) -> dict[str, Any]:
    with profile_path(name).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Profile {name} must be a YAML mapping")
    return data


def set_active_profile(name: str) -> dict[str, Any]:
    """Persist profile choice for the next bot start."""
    name = str(name).strip().lower()
    profile_path(name)  # validate
    os.environ[ENV_VAR] = name
    state = {
        "profile": name,
        "path": str(PROFILES_DIR / f"{name}.yaml"),
    }
    from core.utils import utc_now_iso
    state["updated_at"] = utc_now_iso()
    write_json_state(STATE_FILE, state)
    return state


def profile_summary(name: str) -> dict[str, Any]:
    overlay = load_profile_overlay(name)
    micro = (overlay.get("practice") or {}).get("micro") or {}
    return {
        "name": name,
        "label": overlay.get("label", name),
        "description": overlay.get("description", ""),
        "account_size_usd": micro.get("account_size_usd"),
        "micro_enabled": micro.get("enabled"),
        "symbols": micro.get("symbols") or overlay.get("practice", {}).get("symbols"),
    }