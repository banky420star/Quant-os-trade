"""Persistent archive of approved signals — survives cycle-to-cycle rotation."""

from __future__ import annotations

from typing import Any

from core.utils import read_json_state, utc_now_iso, write_json_state

ARCHIVE_FILE = "signal_archive.json"
MAX_ARCHIVE = 5000


def load_archive() -> dict[str, Any]:
    return read_json_state(ARCHIVE_FILE, default={"signals": {}, "history": []}) or {}


def archive_signals(signals: list[dict[str, Any]], *, source: str = "verifier") -> int:
    """Append approved signals by signal_id for later trade enrichment."""
    if not signals:
        return 0
    doc = load_archive()
    index: dict[str, Any] = dict(doc.get("signals") or {})
    added = 0
    now = utc_now_iso()
    for sig in signals:
        sid = sig.get("signal_id")
        if not sid:
            continue
        payload = dict(sig)
        payload["archived_at"] = now
        payload["archive_source"] = source
        if sid not in index:
            added += 1
        index[sid] = payload
    if len(index) > MAX_ARCHIVE:
        # Drop oldest by archived_at
        ordered = sorted(
            index.items(),
            key=lambda kv: (kv[1].get("archived_at") or ""),
        )
        index = dict(ordered[-MAX_ARCHIVE:])
    history = list(doc.get("history") or [])
    if added:
        history.append({"ts": now, "source": source, "count": added, "total": len(index)})
        history = history[-200:]
    write_json_state(ARCHIVE_FILE, {
        "updated_at": now,
        "signals": index,
        "count": len(index),
        "history": history,
    })
    return added


def lookup_archive(signal_id: str | None) -> dict[str, Any] | None:
    if not signal_id:
        return None
    doc = load_archive()
    sig = (doc.get("signals") or {}).get(str(signal_id))
    return dict(sig) if isinstance(sig, dict) else None