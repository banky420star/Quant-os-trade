"""Account-size profile launcher — apply 30 / 100 / growth overlays."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from core.utils import PROJECT_ROOT, read_json_state, utc_now_iso, write_json_state

PROFILES_DIR = PROJECT_ROOT / "profiles"
STATE_FILE = "active_profile.json"
ENV_VAR = "MT5_QUANT_PROFILE"
DEFAULT_PROFILE = "30"
REAL_ACCOUNT_PROFILE = "live"
BALANCE_TIERS: tuple[tuple[float, str], ...] = (
    (75.0, "30"),
    (250.0, "100"),
    (float("inf"), "growth"),
)


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


def _load_base_config() -> dict[str, Any]:
    """Load config.yaml (+ local) without profile overlay — for MT5 probe only."""
    config_path = PROJECT_ROOT / "config.yaml"
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    local_path = PROJECT_ROOT / "config.local.yaml"
    if local_path.is_file():
        with local_path.open("r", encoding="utf-8") as handle:
            local = yaml.safe_load(handle) or {}
        if isinstance(local, dict):
            from core.utils import _deep_merge
            config = _deep_merge(config, local)
    return config if isinstance(config, dict) else {}


def probe_logged_in_account(
    config: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any] | None:
    """Attach to the MT5 terminal session and read the logged-in account."""
    log = logger or logging.getLogger("profile_launcher")
    try:
        from core.mt5_connection_manager import MT5ConnectionManager
    except ImportError:
        return None

    base = config or _load_base_config()
    conn = MT5ConnectionManager(base, log)
    try:
        conn.connect()
        snap = conn.account_snapshot()
    except Exception as exc:  # noqa: BLE001
        log.debug("MT5 profile probe skipped: %s", exc)
        return None
    finally:
        try:
            conn.disconnect()
        except Exception:  # noqa: BLE001
            pass

    if not snap.get("login"):
        return None

    account = {
        "timestamp": utc_now_iso(),
        "login": snap["login"],
        "server": snap.get("server"),
        "balance": snap.get("balance"),
        "equity": snap.get("equity", snap.get("balance")),
        "currency": snap.get("currency"),
        "account_mode": str(snap.get("account_mode") or "demo").lower(),
        "trade_allowed": snap.get("trade_allowed"),
        "source": "mt5_probe",
    }
    write_json_state("account.json", account)
    return account


def resolve_profile_for_account(account: dict[str, Any]) -> str:
    """Map a logged-in MT5 account snapshot to a profile name."""
    account_mode = str(account.get("account_mode") or "demo").lower()
    if account_mode == "real":
        return REAL_ACCOUNT_PROFILE

    equity = float(account.get("equity") or account.get("balance") or 0)
    for max_equity, profile in BALANCE_TIERS:
        if equity <= max_equity:
            return profile
    return DEFAULT_PROFILE


def auto_select_profile(
    *,
    explicit: str | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Pick and activate the profile for the currently logged-in MT5 account.

    Priority:
      1. explicit CLI argument (--profile)
      2. MT5_QUANT_PROFILE environment variable
      3. Live MT5 terminal probe (logged-in account)
      4. state/account.json from the last session
      5. state/active_profile.json
      6. default profile (30)
    """
    log = logger or logging.getLogger("profile_launcher")
    source = "default"
    account: dict[str, Any] | None = None

    if explicit and explicit.lower() not in ("auto", ""):
        profile = str(explicit).strip().lower()
        source = "cli"
    elif os.environ.get(ENV_VAR, "").strip():
        profile = os.environ[ENV_VAR].strip().lower()
        source = "env"
    else:
        account = probe_logged_in_account(logger=log)
        if account:
            profile = resolve_profile_for_account(account)
            source = "mt5_logged_in"
        else:
            cached = read_json_state("account.json", default={}) or {}
            if cached.get("login"):
                account = cached
                profile = resolve_profile_for_account(cached)
                source = "account_json"
            else:
                state = read_json_state(STATE_FILE, default={}) or {}
                profile = str(state.get("profile") or DEFAULT_PROFILE).strip().lower()
                source = "active_profile_json" if state.get("profile") else "default"

    profile_path(profile)
    state = set_active_profile(profile)
    report = {
        "profile": profile,
        "source": source,
        "active_profile_state": state,
        "account": account,
        "summary": profile_summary(profile),
    }
    log.info(
        "Profile auto-select: %s (source=%s login=%s equity=%s mode=%s)",
        profile,
        source,
        (account or {}).get("login"),
        (account or {}).get("equity") or (account or {}).get("balance"),
        (account or {}).get("account_mode"),
    )
    return report


def apply_logged_in_account_mode(config: dict[str, Any], account: dict[str, Any] | None) -> dict[str, Any]:
    """Sync mt5.account_mode with the terminal's logged-in account type."""
    if not account:
        return config
    mode = str(account.get("account_mode") or "").lower()
    if mode in ("demo", "real", "contest"):
        config.setdefault("mt5", {})["account_mode"] = mode
    return config