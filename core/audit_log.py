"""Append-only operational audit trail for trading events."""

from __future__ import annotations

import json
from typing import Any

from core.utils import STATE_DIR, utc_now_iso

AUDIT_PATH = STATE_DIR / "audit_log.jsonl"
_MAX_LINES = 5000


def append_event(event: str, *, symbol: str | None = None, details: dict[str, Any] | None = None) -> None:
    """Record one immutable audit line (JSONL)."""
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": utc_now_iso(),
        "event": event,
    }
    if symbol:
        row["symbol"] = symbol
    if details:
        row["details"] = details
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
    _trim_if_needed()


def _trim_if_needed() -> None:
    if not AUDIT_PATH.exists():
        return
    try:
        lines = AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= _MAX_LINES:
        return
    keep = lines[-_MAX_LINES:]
    AUDIT_PATH.write_text("\n".join(keep) + "\n", encoding="utf-8")