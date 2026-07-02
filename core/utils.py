"""Shared utilities for config, logging, and state I/O."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"
LOGS_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"

_STATE_LOCKS: dict[str, threading.Lock] = {}
_STATE_LOCKS_GUARD = threading.Lock()


def _state_lock(filename: str) -> threading.Lock:
    with _STATE_LOCKS_GUARD:
        if filename not in _STATE_LOCKS:
            _STATE_LOCKS[filename] = threading.Lock()
        return _STATE_LOCKS[filename]


def ensure_dirs() -> None:
    """Create required runtime directories."""
    for path in (STATE_DIR, LOGS_DIR, DATA_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load YAML configuration, optionally overlaid with config.local.yaml."""
    config_path = path or (PROJECT_ROOT / "config.yaml")
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if path is None:
        local_path = PROJECT_ROOT / "config.local.yaml"
        if local_path.exists():
            with local_path.open("r", encoding="utf-8") as handle:
                local = yaml.safe_load(handle) or {}
            if isinstance(local, dict):
                config = _deep_merge(config, local)
    if isinstance(config, dict):
        from core.account_mode import performance_gates_active
        from core.blue_guardian import (
            apply_config_overrides,
            blue_guardian_enabled,
            prepare_blue_guardian_profile,
        )
        from core.performance_benchmark import sync_performance_gates
        from core.practice_session import sync_practice_gates

        if blue_guardian_enabled(config):
            config = prepare_blue_guardian_profile(config)
        if config.get("performance"):
            config = sync_performance_gates(config)
        if not performance_gates_active(config):
            config = sync_practice_gates(config)
        config = apply_config_overrides(config)
    return config


def setup_logger(name: str, log_file: str, level: int = logging.INFO) -> logging.Logger:
    """Configure a file + console logger."""
    ensure_dirs()
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOGS_DIR / log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def read_json_state(filename: str, default: Any = None) -> Any:
    """Read JSON state file; return default if missing."""
    path = STATE_DIR / filename
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_state(filename: str, data: Any) -> Path:
    """Write JSON state file atomically with per-file locking and Windows-safe retries.

    Supports nested relative paths (e.g. ``culturing/XAUUSDm.json``) by creating
    parent directories under STATE_DIR. Top-level filenames are unaffected (their
    parent is STATE_DIR, which already exists).
    """
    ensure_dirs()
    path = STATE_DIR / filename
    # Create nested parent dirs (no-op for top-level files). Done before the
    # write loop so a missing parent is a one-shot setup, not a per-retry error.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, indent=2, default=str)

    with _state_lock(filename):
        last_err: OSError | None = None
        for attempt in range(10):
            try:
                tmp_path.write_text(payload, encoding="utf-8")
                os.replace(tmp_path, path)
                return path
            except OSError as exc:
                last_err = exc
                if attempt < 9:
                    time.sleep(0.04 * (2 ** attempt))
                else:
                    try:
                        path.write_text(payload, encoding="utf-8")
                        tmp_path.unlink(missing_ok=True)
                        return path
                    except OSError:
                        raise last_err from exc
        if last_err:
            raise last_err
    return path


def fail_safe_missing(filename: str, logger: logging.Logger) -> bool:
    """Log and return True if required input file is missing."""
    path = STATE_DIR / filename
    if not path.exists():
        logger.error("Required input missing: %s", path)
        return True
    return False