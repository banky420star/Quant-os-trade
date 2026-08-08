"""Shared utilities for config, logging, and state I/O."""

from __future__ import annotations

import hashlib
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
# Validation workers can override state I/O for their child test process. The
# normal bot path remains rooted at <project>/state; only the explicit test
# environment variable redirects it to a temporary directory.
_STATE_DIR_OVERRIDE = os.environ.get("MT5_QUANT_TEST_STATE_DIR")
STATE_DIR = Path(_STATE_DIR_OVERRIDE) if _STATE_DIR_OVERRIDE else PROJECT_ROOT / "state"
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
        from core.profile_launcher import active_profile_name, load_profile_overlay
        profile_name = active_profile_name()
        if profile_name:
            try:
                overlay = load_profile_overlay(profile_name)
                config = _deep_merge(config, overlay)
                config["active_profile"] = profile_name
            except (FileNotFoundError, ValueError, OSError):
                pass
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
        from core.micro_profile import sync_micro_profile
        from core.strategy_arena import sync_arena_symbols
        config = sync_arena_symbols(config)
        config = sync_micro_profile(config)
        if not performance_gates_active(config):
            config = sync_practice_gates(config)
        config = apply_config_overrides(config)
        # Phase 2.4: apply bounded learning overrides ONLY in live_apply_limited
        # mode so the review loop's safety-checked patches reach the decision
        # path. observe_only/review_only/propose_only/shadow_apply never mutate
        # the live config. Lazy import avoids a circular import at module load.
        if (config.get("learning") or {}).get("mode") == "live_apply_limited":
            try:
                from core.learning_overrides import apply_learning_overrides
                config, _applied = apply_learning_overrides(config)
            except Exception:
                pass
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
    """Read JSON state file; return default if missing or unreadable."""
    path = STATE_DIR / filename
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = handle.read().strip()
        if not raw:
            return default
        return json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return default


def _agent_lock_allows_write(filename: str) -> bool:
    """Only the canonical start.py PID may write contested orchestration state."""
    if filename not in ("supervisor.json", "runtime_mode.json"):
        return True
    lock = read_json_state("agent_lock.json", default={}) or {}
    lock_pid = int(lock.get("pid") or 0)
    if not lock_pid:
        return True
    return lock_pid == os.getpid()


def write_json_state(filename: str, data: Any) -> Path | None:
    """Write JSON state file atomically with per-file locking and Windows-safe retries.

    Supports nested relative paths (e.g. ``culturing/XAUUSDm.json``) by creating
    parent directories under STATE_DIR. Top-level filenames are unaffected (their
    parent is STATE_DIR, which already exists).

    Atomicity is absolute (2026-08-08, Agent 6): the target path is ONLY ever
    updated via ``os.replace`` of a fully-written temp file. There is NO
    fallback to writing the target directly — a reader must never observe a
    half-written JSON file, especially at the moment of a lock/replace failure
    where state integrity matters most. If every retry fails, this function
    fails CLOSED: it logs the error, removes the stale temp file, keeps the
    last valid state on disk, and returns None (callers treat the return as
    advisory; the old state remains valid — possibly stale — until a later
    write succeeds).
    """
    if not _agent_lock_allows_write(filename):
        return STATE_DIR / filename
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
        logging.getLogger("utils").error(
            "write_json_state FAILED after 10 attempts filename=%s err=%s — "
            "kept previous state; target NOT updated (no non-atomic fallback)",
            filename, last_err,
        )
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def fail_safe_missing(filename: str, logger: logging.Logger) -> bool:
    """Log and return True if required input file is missing."""
    path = STATE_DIR / filename
    if not path.exists():
        logger.error("Required input missing: %s", path)
        return True
    return False


# ---- JSONL archive helpers (Tier-2 mgmt archive) -----------------------------
# Append-only per-line JSON records under state/<filename_without_ext>.jsonl.
# Used by core/position_manager.py to append the final mgmt_row to
# state/position_mgmt_archive.jsonl BEFORE the live state rolls, so
# core/trade_tracker.py can recover BE/Partial/Stale flags at merge time.
# Schema (one record per line):
#   {
#     "ticket": "123456",
#     "transaction_id": "<8-char hex>",
#     "reason": "be_trail_update" | "partial_tp" | "time_stop" | "mt5_close_sync" | "backfill",
#     "archived_at": "<utc iso>",
#     "side": "BUY" | "SELL",
#     "entry": float,
#     "symbol": "XAUUSDm",
#     "mgmt_row": { "break_even": bool, "trailing": bool, "partial_tp_done": bool,
#                    "initial_sl": float, "peak_price": float, "trail_distance": float,
#                    "risk_distance_floor": float, "stale_closed": bool,
#                    "_audit": [ ... ] }
#   }
ARCHIVE_DEFAULT_MAX_LINES = 50000


def _archive_path(filename: str) -> Path:
    """Resolve state/<archive_basename>.jsonl from a filename like 'foo.jsonl'
    OR a stem-only name like 'foo' (auto-appends .jsonl)."""
    if not filename:
        raise ValueError("archive filename must be non-empty")
    if filename.endswith(".jsonl"):
        return STATE_DIR / filename
    return STATE_DIR / f"{filename}.jsonl"


def append_archive_record(
    filename: str,
    record: dict[str, Any],
    *,
    max_lines: int = ARCHIVE_DEFAULT_MAX_LINES,
) -> Path:
    """Append a single JSON record (one line) to state/<stem>.jsonl. Atomic
    per-line write under the same _state_lock used by write_json_state.

    Idempotency key: ``record["transaction_id"]``. If a record with the same
    transaction_id already exists in the file, this call is a no-op.

    Auto-prunes the file to ``max_lines`` after an append (keeps the tail).
    Returns the path written (path may exist but be unchanged on dedup hit).
    """
    if not isinstance(record, dict):
        raise TypeError("archive record must be a dict")
    ensure_dirs()
    path = _archive_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    full_record = dict(record)
    full_record.setdefault("archived_at", utc_now_iso())
    line = json.dumps(full_record, default=str, separators=(",", ":"))
    with _state_lock(str(path.name)):
        # Dedup check (cheap; archive is bounded by max_lines).
        existing_tx = _read_archive_txindex(path)
        txid = full_record.get("transaction_id")
        if txid and txid in existing_tx:
            return path
        last_err: OSError | None = None
        for attempt in range(10):
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                break
            except OSError as exc:
                last_err = exc
                if attempt < 9:
                    time.sleep(0.04 * (2 ** attempt))
                else:
                    if last_err:
                        raise last_err
        if max_lines > 0 and max_lines < ARCHIVE_DEFAULT_MAX_LINES * 100:
            _prune_archive_tail(path, max_lines=max_lines)
    return path


def _read_archive_txindex(path: Path) -> set[str]:
    """Return the set of transaction_id values currently in the archive."""
    if not path.exists():
        return set()
    txids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                txid = obj.get("transaction_id")
                if txid:
                    txids.add(str(txid))
    except OSError:
        return set()
    return txids


def _prune_archive_tail(path: Path, *, max_lines: int) -> None:
    """Keep only the last ``max_lines`` non-empty lines. Atomic via temp+rename."""
    if max_lines <= 0 or not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as handle:
            lines = [ln for ln in handle.readlines() if ln.strip()]
    except OSError:
        return
    if len(lines) <= max_lines:
        return
    keep = lines[-max_lines:]
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text("".join(keep), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_archive_index(
    filename: str,
    *,
    key: str = "ticket",
    latest_per_key: bool = True,
) -> dict[str, dict[str, Any]]:
    """Read state/<stem>.jsonl into a dict keyed by ``record[key]``. When
    ``latest_per_key`` is True (default), later records overwrite earlier
    ones so the most recent mgmt state per ticket wins. Skip records that
    decode as non-dict or that lack the key.
    """
    path = _archive_path(filename)
    if not path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                k = obj.get(key)
                if k is None:
                    continue
                if not latest_per_key:
                    out.setdefault(str(k), obj)
                else:
                    out[str(k)] = obj
    except OSError:
        return {}
    return out


def read_archive_for_ticket(
    filename: str,
    ticket: str | int,
) -> dict[str, Any] | None:
    """Return the most-recent archive record for ``ticket`` or None."""
    return read_archive_index(filename).get(str(ticket))


def archive_line_count(filename: str) -> int:
    """Count non-empty lines in state/<stem>.jsonl. Used by tests + backfill."""
    path = _archive_path(filename)
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for ln in handle if ln.strip())
    except OSError:
        return 0


# ---- Shared lookup helpers (2026-07-20) -------------------------------------
# Both trade_tracker._hydrate_mgmt_from_archive AND dashboard
# ::_build_profit_quality use the same predicate: "does this live mgmt row have
# any truthy flag?" — DUPLICATING this twice risks silent drift when one site
# tightens the rule and the other doesn't. Single source of truth below.
def _live_mgmt_has_truthy(mgmt: dict[str, Any] | None) -> bool:
    """True iff ``mgmt`` has at least one truthy value (skipping None).
    Used to decide whether to consult the archive as a fallback for closed
    positions whose live mgmt row has no resolvable flags.
    Used at:
      - core/trade_tracker.py::_hydrate_mgmt_from_archive.at-close time
      - dashboard/server.py::_build_profit_quality at dashboard-poll time
    """
    if not mgmt:
        return False
    return any(bool(v) for v in mgmt.values() if v is not None)


# Module-level mtime cache for read_archive_index to avoid parsing the whole
# JSONL on every dashboard poll or sync_mt5_closed_deals tick. Bounded by
# state/<stem>.jsonl mtime; readers see a stale snapshot only between mtime
# updates. Cache key is (filename, key, latest_per_key) so callers with
# different ``latest_per_key`` flags don't share stale results (REVIEW FIX 1).
_ARCHIVE_INDEX_CACHE: dict[tuple[str, str, bool], tuple[float, dict[str, dict[str, Any]]]] = {}


def read_archive_index_cached(
    filename: str,
    *,
    key: str = "ticket",
    latest_per_key: bool = True,
) -> dict[str, dict[str, Any]]:
    """Mtime-cached variant of :func:`read_archive_index`. Mirrors the
    semantics of trade_tracker._ARCHIVE_INDEX_CACHE so the dashboard / the
    at-close join / and any future consumer all read the same index."""
    path = _archive_path(filename)
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        mtime = 0.0
    cache_key = (filename, key, latest_per_key)
    cached = _ARCHIVE_INDEX_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]
    index = read_archive_index(filename, key=key, latest_per_key=latest_per_key)
    _ARCHIVE_INDEX_CACHE[cache_key] = (mtime, index)
    return index


# ---- Payoff Paradox audit JSONL helpers (2026-07-20) -------------------------
# Append-only JSONL at state/payoff_paradox_audit.jsonl. One row per CLOSED
# trade; written by core/trade_tracker.py on detect_paper_closed AND
# sync_mt5_closed_deals. Used by scripts/fit_payoff_paradox_floor.py as the
# source of truth for self-tuning the BE-floor (``trading.exits.
# min_r_multiple_win``). Schema (one record per line):
#   {
#     "ts": "<utc iso close time>",
#     "symbol": str,
#     "side": "BUY"|"SELL",
#     "entry": float,
#     "exit": float,
#     "sl": float|None,                # initial_sl from trade record
#     "r": float,                       # realised R = sign-aligned exit diff / risk
#     "would_have_been_locked_at": float|None,  # BE-floor lock price for that floor
#     "projected_floor": float,         # min_r_multiple_win in effect at close
#     "result": "win"|"loss",
#     "pnl": float,
#     "ticket": str,
#     "transaction_id": "<12-char hex of sha1(ts|ticket)>",  # dedup key
#     "archived_at": "<utc iso>",
#   }
# Dedup: (ts, ticket) is the natural key — a bot re-running sync_mt5_closed
# will not double-append the same deal. We hash to a 12-char hex via
# hashlib.sha1 so the dedup index stays small.
PAYOFF_PARADOX_DEFAULT_FILENAME = "payoff_paradox_audit.jsonl"
PAYOFF_PARADOX_DEFAULT_MAX_LINES = 200000  # bounded (~0.5 MB at ~2.5 KB/line)


def _payoff_paradox_path(filename: str = PAYOFF_PARADOX_DEFAULT_FILENAME) -> Path:
    if not filename:
        raise ValueError("payoff_paradox filename must be non-empty")
    if filename.endswith(".jsonl"):
        return STATE_DIR / filename
    return STATE_DIR / f"{filename}.jsonl"


def _payoff_paradox_tx(record: dict[str, Any]) -> str:
    """Deterministic dedup key for the payoff_paradox audit log — (ts, ticket).
    Stable across bot restarts, sync_mt5 re-runs, and any number of redos."""
    ts = str(record.get("ts") or record.get("close_ts") or "")
    ticket = str(record.get("ticket") or record.get("position_id") or "")
    raw = f"{ts}|{ticket}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _payoff_paradox_read_txindex(path: Path) -> set[str]:
    if not path.exists():
        return set()
    txids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                txid = obj.get("transaction_id")
                if txid:
                    txids.add(str(txid))
    except OSError:
        return set()
    return txids


# REVIEW FIX 2 (perf): the close-cycle hot path used to re-walk the entire
# JSONL on EVERY append to build the dedup txid set. With ~50k rows × ~2 KB
# that's ~2 sec of pure I/O per close batch. Same mtime-cache pattern as
# _ARCHIVE_INDEX_CACHE keeps the close cycle O(1) on the dedup look-up.
_PAYOFF_PARADOX_TX_CACHE: dict[str, tuple[float, set[str]]] = {}


def _payoff_paradox_read_txindex_cached(path: Path) -> set[str]:
    """Mtime-cached dedup-index read.

    Returns a frozenset-like live object — we re-parse only when the file's
    mtime advances, so appends inside the same mtime bucket are O(1) dedup
    hits. The cache key is the path string; each filename keeps its own slot.
    """
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        mtime = 0.0
    cache_key = str(path)
    cached = _PAYOFF_PARADOX_TX_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]
    tx_set = _payoff_paradox_read_txindex(path)
    _PAYOFF_PARADOX_TX_CACHE[cache_key] = (mtime, tx_set)
    return tx_set


def _invalidate_payoff_paradox_tx_cache(filename: str) -> None:
    """Drop the cache entry for ``filename`` after any external mutation
    (backfill, restore, etc.) so the next read picks up the new mtime/contents."""
    _PAYOFF_PARADOX_TX_CACHE.pop(filename, None)
    _PAYOFF_PARADOX_AUDIT_CACHE.pop(filename, None)


def append_payoff_paradox_audit(
    record: dict[str, Any],
    *,
    filename: str = PAYOFF_PARADOX_DEFAULT_FILENAME,
    max_lines: int = PAYOFF_PARADOX_DEFAULT_MAX_LINES,
) -> Path | None:
    """Append a single audit row to state/<stem>.jsonl.

    Idempotent via ``transaction_id`` (sha1 hash of ``ts|ticket``). Returns
    the path on success; returns None on any recoverable failure — the caller
    MUST keep going even when the audit is unreadable.
    """
    if not isinstance(record, dict):
        logging.getLogger("utils").warning(
            "Payoff paradox append skipped: non-dict record (%s)",
            type(record).__name__,
        )
        return None
    if not record.get("ts") or not record.get("symbol"):
        # We require at least ts + symbol so the JSONL row has a stable dedup
        # key and a usable aggregation handle.
        logging.getLogger("utils").warning(
            "Payoff paradox append skipped: missing ts/symbol keys (have=%s)",
            sorted(record.keys()),
        )
        return None
    full = dict(record)
    txid = full.get("transaction_id") or _payoff_paradox_tx(full)
    full["transaction_id"] = txid
    full.setdefault("archived_at", utc_now_iso())
    line = json.dumps(full, default=str, separators=(",", ":"))
    path = _payoff_paradox_path(filename)
    ensure_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _state_lock(str(path.name)):
            existing_tx = _payoff_paradox_read_txindex_cached(path)
            if txid in existing_tx:
                return path
            last_err: OSError | None = None
            for attempt in range(10):
                try:
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                        handle.flush()
                    break
                except OSError as exc:
                    last_err = exc
                    if attempt < 9:
                        time.sleep(0.04 * (2 ** attempt))
                    else:
                        logging.getLogger("utils").warning(
                            "Payoff paradox audit append FAILED path=%s err=%s",
                            path, last_err,
                        )
                        return None
            if max_lines > 0 and max_lines < PAYOFF_PARADOX_DEFAULT_MAX_LINES * 100:
                _prune_archive_tail(path, max_lines=max_lines)
    except (OSError, ValueError, TypeError) as exc:
        logging.getLogger("utils").warning(
            "Payoff paradox audit append FAILED err=%s", exc,
        )
        return None
    return path


def read_payoff_paradox_audit(
    filename: str = PAYOFF_PARADOX_DEFAULT_FILENAME,
) -> list[dict[str, Any]]:
    """Read all rows from state/<stem>.jsonl as a list of dicts. Lines that
    fail to parse are skipped silently (the file may be mid-append)."""
    path = _payoff_paradox_path(filename)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


# Mtime cache for the audit-index read path so the dashboard / fitter can
# poll without re-parsing the JSONL. Same pattern as read_archive_index_cached.
_PAYOFF_PARADOX_AUDIT_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def read_payoff_paradox_audit_cached(
    filename: str = PAYOFF_PARADOX_DEFAULT_FILENAME,
) -> list[dict[str, Any]]:
    """Mtime-cached variant of :func:`read_payoff_paradox_audit`."""
    path = _payoff_paradox_path(filename)
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        mtime = 0.0
    cached = _PAYOFF_PARADOX_AUDIT_CACHE.get(filename)
    if cached and cached[0] == mtime:
        return cached[1]
    rows = read_payoff_paradox_audit(filename)
    _PAYOFF_PARADOX_AUDIT_CACHE[filename] = (mtime, rows)
    return rows


def reset_payoff_paradox_audit_caches_for_tests() -> None:
    """Drop both the audit-row cache AND the tx-dedup cache. Tests call this
    when they mutate the file directly (write outside the appender) so the
    next read picks up the latest state."""
    _PAYOFF_PARADOX_AUDIT_CACHE.clear()
    _PAYOFF_PARADOX_TX_CACHE.clear()


def payoff_paradox_audit_line_count(
    filename: str = PAYOFF_PARADOX_DEFAULT_FILENAME,
) -> int:
    """Count non-empty lines in state/<stem>.jsonl. Used by tests/backfill."""
    path = _payoff_paradox_path(filename)
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for ln in handle if ln.strip())
    except OSError:
        return 0


# ---- In-memory layer AST discovery (2026-07-21) -----------------------------
# Replaces the static 8-entry _IN_MEMORY_LAYER_CATALOG_TEMPLATE. Walks
# core/utils.py:load_config with `ast`, surfaces every post-YAML sync_ /
# apply_ / prepare_ / activate_ call, and extracts the surrounding `if (…)`
# guard into a runtime-evaluable predicate. A future engineer adding a new
# sync_xyz(config) step inside load_config's `if isinstance(config, dict):`
# region is automatically surfaced by /api/cli_overrides without any edit
# to dashboard/server.py.
#
# Safety-relevance is derived from func-name keywords
# (guardian / config_overrides / learning_overrides / kill_switch /
# emergency_exit default True; everything else False). Runtime override
# via state/in_memory_layer_safety.json if present.
import ast as _ast_mod  # local alias keeps the import minimal
_INMEM_DISCOVERY_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_INMEM_DISCOVERY_CACHE_LOCK = threading.Lock()
_INMEM_SAFETY_KEYWORDS = (
    "guardian",
    "config_overrides",
    "learning_overrides",
    "kill_switch",
    "emergency_exit",
)


def _inmem_is_safety_relevant(func_name: str) -> bool:
    fn = (func_name or "").lower()
    return any(kw in fn for kw in _INMEM_SAFETY_KEYWORDS)


def _inmem_collect_guard_names(guard_str: str) -> set[str]:
    """Return the set of free Name identifiers referenced in the guard."""
    names: set[str] = set()
    try:
        guard_ast = _ast_mod.parse(guard_str, mode="eval")
    except SyntaxError:
        return names
    for node in _ast_mod.walk(guard_ast):
        if isinstance(node, _ast_mod.Name):
            names.add(node.id)
    return names


def _inmem_compile_predicate(
    guard_str: str, imports: dict[str, str]
) -> object | None:
    """Compile an AST guard string into a callable predicate lambda c -> bool.

    Uses ast.parse + ast.Lambda + compile + eval (NEVER arbitrary string
    exec) so a hostile guard expression can't reach the filesystem. Names
    referenced in the guard are resolved against the import map; missing
    names fall through to KeyError at call time so the safety_relevant
    default kicks in.
    """
    text = (guard_str or "").strip()
    if not text or text == "True":
        try:
            return eval("lambda c: True", {"__builtins__": {}})
        except Exception:
            return None
    if text == "False":
        try:
            return eval("lambda c: False", {"__builtins__": {}})
        except Exception:
            return None
    try:
        guard_ast = _ast_mod.parse(text, mode="eval")
    except SyntaxError:
        return None
    # Rename `config` -> `c` (the predicate's parameter) only at Name nodes.
    # Substring-mention of "config" inside function or attribute names is
    # untouched because those are Attribute.func, not Name.load.
    for node in _ast_mod.walk(guard_ast):
        if isinstance(node, _ast_mod.Name) and node.id == "config":
            node.id = "c"
    try:
        lambda_ast = _ast_mod.Expression(
            body=_ast_mod.Lambda(
                args=_ast_mod.arguments(
                    posonlyargs=[],
                    args=[_ast_mod.arg(arg="c")],
                    kwonlyargs=[],
                    vararg=None,
                    kwarg=None,
                    defaults=[],
                    kw_defaults=[],
                ),
                body=guard_ast.body,
            )
        )
        _ast_mod.fix_missing_locations(lambda_ast)
        code = compile(lambda_ast, "<in_mem_predicate>", "eval")
        safe_globals: dict[str, Any] = {"__builtins__": {}}
        # Resolve referenced names via the import map. Each name falls back to
        # None if the import fails so a runtime KeyError triggers the
        # safety_relevant-aware default in _compute_in_memory_layers.
        for nm in _inmem_collect_guard_names(text):
            if nm == "c":
                continue
            module_name = imports.get(nm)
            if not module_name:
                continue
            try:
                mod = __import__(module_name, fromlist=[nm])
                safe_globals[nm] = getattr(mod, nm, None)
            except Exception:
                pass
        return eval(code, safe_globals)
    except Exception:
        return None


def _inmem_resolve_call_module(
    tree: _ast_mod.AST, callee: _ast_mod.AST, imports: dict[str, str]
) -> str:
    """Best-effort qualified module path for a Call's callee.

    `Name` calls use `imports.get(func_name, 'core.utils')`.
    `Attribute` calls like `foo.sync_xyz(config)` use
    `imports.get('foo', 'core.utils')`.
    """
    if isinstance(callee, _ast_mod.Name):
        return imports.get(callee.id, "core.utils")
    if isinstance(callee, _ast_mod.Attribute):
        target = callee.value
        if isinstance(target, _ast_mod.Name):
            return imports.get(target.id, "core.utils")
    return "core.utils"


def discover_in_memory_layers(
    path: Path | str | None = None,
    *,
    safety_overrides: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Walk core/utils.py:load_config and surface every post-YAML in-memory
    sync/apply/prepare call. Returns a list of dicts shaped to feed
    dashboard/server.py::_compute_in_memory_layers.

    Each entry: dict with keys
      - name: `in_memory:<func>[@<lineno> | _pre | _post]`
      - source_path: `<module-path>.py:<func_name>`
      - func_name: bare function name
      - predicate_str: Python expression evaluated against `config` (caller
        supplies the lambda via _inmem_compile_predicate)
      - safety_relevant: bool from _inmem_is_safety_relevant (overridable
        via safety_overrides map keyed by func_name)

    Mtime-cached so a dashboard poll hot loop doesn't re-parse the AST
    on every tick. Returns [] on parse failure (caller falls back to the
    static template).
    """
    p = Path(path) if path else (PROJECT_ROOT / "core" / "utils.py")
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return []
    cached = _INMEM_DISCOVERY_CACHE.get(str(p))
    if cached and cached[0] == mtime and safety_overrides is None:
        return cached[1]

    try:
        src = p.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = _ast_mod.parse(src)
    except SyntaxError as exc:
        logging.getLogger("utils").warning(
            "discover_in_memory_layers: ast.parse failed: %s", exc
        )
        return []

    # Build import map {local_name: module} from top-of-file imports.
    imports_local: dict[str, str] = {}
    for node in _ast_mod.iter_child_nodes(tree):
        if isinstance(node, _ast_mod.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                imports_local[local] = node.module

    # Locate load_config's body.
    load_cfg = None
    for node in _ast_mod.iter_child_nodes(tree):
        if isinstance(node, _ast_mod.FunctionDef) and node.name == "load_config":
            load_cfg = node
            break
    if load_cfg is None:
        return []

    # The unique post-YAML region is the `if isinstance(config, dict):` block
    # at the tail of load_config. Locate it; if absent (file changed), fall
    # back to the entire body so a renamed guard doesn't kill discovery.
    post_yaml_body: list = []
    for stmt in load_cfg.body:
        if isinstance(stmt, _ast_mod.If):
            test_src = ""
            try:
                test_src = _ast_mod.unparse(stmt.test)
            except Exception:
                test_src = ""
            if "isinstance" in test_src and "config" in test_src and "dict" in test_src:
                post_yaml_body = stmt.body
                break
    if not post_yaml_body:
        return []

    catalog: list[dict[str, Any]] = []
    func_call_index: dict[str, int] = {}

    def _walk(stmts: list[_ast_mod.stmt], guard_str: str) -> None:
        for stmt in stmts:
            if isinstance(stmt, _ast_mod.If):
                try:
                    inner = _ast_mod.unparse(stmt.test)
                except Exception:
                    inner = ""
                combined = (
                    f"({guard_str}) and ({inner})"
                    if guard_str != "True"
                    else f"({inner})"
                )
                _walk(stmt.body, combined)
                if stmt.orelse:
                    _walk(stmt.orelse, f"not ({inner})")
                continue
            # Recurse into try blocks so in-memory steps wrapped in
            # except-pass guard (e.g. apply_learning_overrides) are not
            # silently dropped. handlers are NOT walked by design — except
            # handlers rarely host sync_* calls and reporting them as
            # production steps would mislead the dashboard. If a future
            # engineer puts a sync_ in an except handler, the right answer
            # is to surface it via _LOG.warning in the handler itself, not
            # to widen the walker.
            if isinstance(stmt, _ast_mod.Try):
                _walk(stmt.body, guard_str)
                _walk(stmt.finalbody, guard_str)
                continue
            # Deferred `from core.X import Y` inside this block — merge into
            # the import map so subsequent calls in the same scope resolve
            # to the right module rather than falling back to "core.utils".
            if isinstance(stmt, _ast_mod.ImportFrom) and stmt.module:
                for alias in stmt.names:
                    imports_local[alias.asname or alias.name] = stmt.module
                continue
            call_node: _ast_mod.Call | None = None
            if (
                isinstance(stmt, _ast_mod.Assign)
                and isinstance(stmt.value, _ast_mod.Call)
            ):
                call_node = stmt.value
            elif (
                isinstance(stmt, _ast_mod.Expr)
                and isinstance(stmt.value, _ast_mod.Call)
            ):
                call_node = stmt.value
            if call_node is None:
                continue
            func_name: str | None = None
            callee = call_node.func
            if isinstance(callee, _ast_mod.Name):
                func_name = callee.id
            elif isinstance(callee, _ast_mod.Attribute):
                func_name = callee.attr
            if not func_name or not any(
                func_name.startswith(p)
                for p in ("sync_", "apply_", "prepare_", "activate_")
            ):
                continue
            # Must receive config as some argument (positional or keyword).
            arg_strings = []
            try:
                arg_strings.append(_ast_mod.unparse(call_node))
            except Exception:
                pass
            if not any("config" in s for s in arg_strings):
                continue

            idx = func_call_index.get(func_name, 0)
            func_call_index[func_name] = idx + 1
            disambig = ""
            if func_name == "sync_practice_gates":
                disambig = "_pre" if idx == 0 else "_post"
            else:
                disambig = f"@{call_node.lineno}"

            module_path = _inmem_resolve_call_module(tree, callee, imports_local)
            safety = _inmem_is_safety_relevant(func_name)
            if safety_overrides and func_name in safety_overrides:
                safety = bool(safety_overrides[func_name])

            catalog.append({
                "name": f"in_memory:{func_name}{disambig}",
                "source_path": f"{module_path.replace('.', '/')}.py:{func_name}",
                "func_name": func_name,
                "disambig": disambig,
                "predicate_str": guard_str,
                "safety_relevant": safety,
            })

    _walk(post_yaml_body, "True")
    if safety_overrides is None:
        with _INMEM_DISCOVERY_CACHE_LOCK:
            _INMEM_DISCOVERY_CACHE[str(p)] = (mtime, catalog)
    return catalog


def load_in_memory_safety_overrides() -> dict[str, bool]:
    """Optional manual safety_relevant override map loaded from
    state/in_memory_layer_safety.json. Lets a future engineer mark a
    non-default layer as safety-relevant (e.g. a custom propagate_risk
    gate) without changing core/utils.py.

    Schema: ``{"<func_name>": <bool>}`` — e.g.
    ``{"sync_custom_kill": true}``.
    """
    fp = STATE_DIR / "in_memory_layer_safety.json"
    if not fp.exists():
        return {}
    try:
        doc = json.loads(fp.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    out: dict[str, bool] = {}
    for k, v in doc.items():
        if isinstance(k, str) and isinstance(v, bool):
            out[k] = v
    return out



# ---- YAML provenance loader (2026-07-21) -------------------------------------
# Used by dashboard/server.py::_build_cli_overrides so the user can see WHICH
# file—and at WHAT line—a setting came from. Without this, base+profile+local
# merges silently shadow each other (e.g. config.local.yaml port=8081 vs
# config.yaml port=8080). The walker relies on PyYAML's compose+walk pairing:
# `yaml.compose` returns Node objects with start_mark.line; SafeLoad gives us
# the resolved Python values. We walk both trees side by side, which is safe
# because Python dicts preserve insertion order (3.7+) and PyYAML preserves
# the YAML source order in MappingNode.value.
def _yaml_load_with_lines(path: Path | str) -> tuple[Any, dict[str, int]]:
    """Load YAML and produce (value, line_map) where line_map["dot.path"] is
    the 1-indexed source line where that key/value pair was declared.

    Sequence elements get a numeric index suffix (`.0`, `.1`, …) so the user
    can pin individual list items back to the source line. Root is recorded
    as ``<root>``. Empty / non-mapping root returns `({}, {})`.

    KNOWN LIMITATION: YAML anchors (``<<: *alias`` / ``*foo``) are reported
    at the *use site* rather than the anchor's declaration line. PyYAML's
    ``yaml.compose`` resolves anchors at parse time so we only see the
    alias node. Acceptable for this codebase since anchor usage is rare;
    flagged here so the UI doesn't promise more than we deliver.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        return {}, {}
    try:
        node = yaml.compose(text)
    except yaml.YAMLError:
        # Fall back to safe_load + empty line map so callers don't 500
        return (yaml.safe_load(text) or {}), {}
    try:
        value = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}, {}
    line_map: dict[str, int] = {}
    _yaml_walk_with_lines(node, value, "", line_map)
    if isinstance(value, (dict, list)):
        return value, line_map
    return value, line_map


def _yaml_walk_with_lines(node, value, path: str, line_map: dict[str, int]) -> None:
    """Side-by-side walker: feed in the composed node + the resolved value;
    record line numbers as we descend; recurse into mappings + sequences.

    MappingNode.value is a list of (key_node, value_node) tuples; the
    resolved Python dict preserves insertion order, so zip(node.value,
    value.items()) correlates pairs in the same order.

    SequenceNode.value is a list of child Nodes; the resolved Python list
    is indexed positionally. Index becomes the path suffix.
    """
    if hasattr(node, "start_mark") and node.start_mark is not None:
        try:
            line_map[path if path else "<root>"] = int(node.start_mark.line) + 1
        except (ValueError, TypeError):
            pass
    if isinstance(node, yaml.MappingNode) and isinstance(value, dict):
        for (k_node, v_node), (k_val, v_val) in zip(node.value, value.items()):
            child_path = f"{path}.{k_val}" if path else str(k_val)
            _yaml_walk_with_lines(v_node, v_val, child_path, line_map)
    elif isinstance(node, yaml.SequenceNode) and isinstance(value, list):
        for i, (v_node, v_val) in enumerate(zip(node.value, value)):
            child_path = f"{path}.{i}" if path else str(i)
            _yaml_walk_with_lines(v_node, v_val, child_path, line_map)