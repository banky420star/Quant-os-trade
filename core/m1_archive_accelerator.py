"""Fast, scope-limited archive appends for the M1 structure ledger.

The generic ``core.utils.append_archive_record`` intentionally rebuilds the
transaction-id index and checks archive pruning on every append. That is safe
for low-volume archives, but the M1 structure engine can emit bursts of
CREATE/CONFIRM/TOUCH/INVALIDATE records inside one 10-second pass. On Windows,
re-reading a growing JSONL for every transition can make a single M1 pass take
well over a minute.

This module accelerates ONLY ``m1_structure_events``. Other archives continue
to use the generic helper unchanged. The cache is validated against file
mtime/size, so an external writer invalidates it automatically. The same
per-file lock and append retry behavior remain in force, and pruning still
occurs once the configured maximum is actually exceeded.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from core import utils as _utils

_TARGETS = {"m1_structure_events", "m1_structure_events.jsonl"}
_CACHE: dict[str, dict[str, Any]] = {}
_ORIGINAL: Callable[..., Path] | None = None


def _signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return int(stat.st_mtime_ns), int(stat.st_size)
    except OSError:
        return 0, 0


def _scan(path: Path) -> tuple[set[str], int]:
    txids: set[str] = set()
    line_count = 0
    if not path.exists():
        return txids, line_count
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                line_count += 1
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                txid = obj.get("transaction_id")
                if txid:
                    txids.add(str(txid))
    except OSError:
        return set(), 0
    return txids, line_count


def _entry(path: Path) -> dict[str, Any]:
    key = str(path.resolve())
    sig = _signature(path)
    cached = _CACHE.get(key)
    if cached is not None and cached.get("signature") == sig:
        return cached

    txids, line_count = _scan(path)
    cached = {
        "signature": _signature(path),
        "txids": txids,
        "line_count": line_count,
    }
    _CACHE[key] = cached
    return cached


def reset_cache() -> None:
    """Clear the process-local M1 archive cache. Primarily for tests."""
    _CACHE.clear()


def append_m1_archive_record(
    filename: str,
    record: dict[str, Any],
    *,
    max_lines: int = _utils.ARCHIVE_DEFAULT_MAX_LINES,
) -> Path:
    """Append one M1 structure record without rescanning the JSONL each time."""
    if filename not in _TARGETS:
        raise ValueError(f"M1 accelerator refuses non-M1 archive {filename!r}")
    if not isinstance(record, dict):
        raise TypeError("archive record must be a dict")

    _utils.ensure_dirs()
    path = _utils._archive_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    full_record = dict(record)
    full_record.setdefault("archived_at", _utils.utc_now_iso())
    line = json.dumps(full_record, default=str, separators=(",", ":"))

    with _utils._state_lock(str(path.name)):
        cache = _entry(path)
        txid = full_record.get("transaction_id")
        txid_s = str(txid) if txid is not None else ""
        if txid_s and txid_s in cache["txids"]:
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
                elif last_err:
                    raise last_err

        if txid_s:
            cache["txids"].add(txid_s)
        cache["line_count"] = int(cache.get("line_count", 0)) + 1
        cache["signature"] = _signature(path)

        # Do not re-read the full archive merely to discover that it is still
        # far below the cap. Prune only when the cached line count crosses it.
        if (
            max_lines > 0
            and max_lines < _utils.ARCHIVE_DEFAULT_MAX_LINES * 100
            and cache["line_count"] > max_lines
        ):
            _utils._prune_archive_tail(path, max_lines=max_lines)
            _CACHE.pop(str(path.resolve()), None)

    return path


def install() -> None:
    """Patch the generic helper only for the M1 structure archive."""
    global _ORIGINAL

    current = _utils.append_archive_record
    if getattr(current, "_m1_structure_accelerated", False):
        return
    _ORIGINAL = current

    def _dispatch(
        filename: str,
        record: dict[str, Any],
        *,
        max_lines: int = _utils.ARCHIVE_DEFAULT_MAX_LINES,
    ) -> Path:
        if filename in _TARGETS:
            return append_m1_archive_record(filename, record, max_lines=max_lines)
        assert _ORIGINAL is not None
        return _ORIGINAL(filename, record, max_lines=max_lines)

    setattr(_dispatch, "_m1_structure_accelerated", True)
    _utils.append_archive_record = _dispatch
