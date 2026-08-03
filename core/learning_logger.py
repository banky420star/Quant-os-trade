"""Append-only normalized JSONL logger for the learning loop (Phase 2.4).

Each log file is a stream of one-line JSON records. Writes are thread-safe and
each file is trimmed to a rolling cap so it never grows unbounded. The logger
records *every* decision (including skips), not only trades.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from core.utils import PROJECT_ROOT, utc_now_iso

LOG_DIR = PROJECT_ROOT / "logs"
MAX_LINES = 8000

_FILES = {
    "decisions": "decisions.jsonl",
    "orders": "orders.jsonl",
    "position_management": "position_management.jsonl",
    "outcomes": "outcomes.jsonl",
    "errors": "errors.jsonl",
    "reviews": "reviews.jsonl",
    "config_changes": "config_changes.jsonl",
    "config_proposals": "config_proposals.jsonl",
    # Phase 2.5 — deterministic-guards observability. Replaces the
    # config_proposals/config_changes audit trail with a single
    # observe-only stream emitted by the log_always guard every cycle.
    "learning_guards": "learning_guards.jsonl",
}

_LOCK = threading.Lock()


def _path(name: str) -> Path:
    return LOG_DIR / _FILES[name]


def _append(name: str, row: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, default=str, ensure_ascii=False) + "\n"
    with _LOCK:
        p = _path(name)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line)
        _trim(name)


def _trim(name: str) -> None:
    p = _path(name)
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if len(lines) <= MAX_LINES:
        return
    p.write_text("\n".join(lines[-MAX_LINES:]) + "\n", encoding="utf-8")


def read_jsonl(name: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Most-recent-first records from a log file."""
    p = _path(name)
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines[-limit * 4 :]):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


def _ts(row: dict[str, Any]) -> dict[str, Any]:
    row.setdefault("timestamp", utc_now_iso())
    return row


def log_decision(event: dict[str, Any]) -> None:
    _append("decisions", _ts(dict(event)))


def log_order(event: dict[str, Any]) -> None:
    _append("orders", _ts(dict(event)))


def log_position_management(event: dict[str, Any]) -> None:
    _append("position_management", _ts(dict(event)))


def log_outcome(event: dict[str, Any]) -> None:
    _append("outcomes", _ts(dict(event)))


def log_error(event: dict[str, Any]) -> None:
    _append("errors", _ts(dict(event)))


def log_review(event: dict[str, Any]) -> None:
    _append("reviews", _ts(dict(event)))


def log_config_change(event: dict[str, Any]) -> None:
    _append("config_changes", _ts(dict(event)))


def log_config_proposal(event: dict[str, Any]) -> None:
    _append("config_proposals", _ts(dict(event)))


def log_guard_report(report: dict[str, Any]) -> None:
    """Append a deterministic-guards snapshot to logs/learning_guards.jsonl.

    Phase 2.5 — emitted every learning-loop cycle so operators can replay
    the guard timeline without needing to parse state/learning_state.json.
    The row carries the verdict of every guard + an 'alerts' list so an
    alerting UI can light up immediately off the JSONL stream.

    This is the AUDIT trail replacement for log_config_proposal +
    log_config_change. It carries zero patch objects — observe-only.
    """
    guards = report.get("guards") or {}
    guards_summary = {
        k: (v.get("verdict") if isinstance(v, dict) else None)
        for k, v in guards.items()
    }
    alerts = [
        {"guard": k, "details": v}
        for k, v in guards.items()
        if isinstance(v, dict) and v.get("verdict") in ("pause", "halve", "alert")
    ]
    row = {
        "timestamp": utc_now_iso(),
        "mode": report.get("mode"),
        "status": report.get("status"),
        "reviewed_trades": report.get("reviewed_trades"),
        "rolling_win_rate_pct": report.get("rolling_win_rate_pct"),
        "rolling_expectancy_r": report.get("rolling_expectancy_r"),
        "guards_summary": guards_summary,
        "alerts": alerts,
    }
    _append("learning_guards", row)


def read_recent_guard_reports(*, limit: int = 50) -> list[dict[str, Any]]:
    """Most-recent-first guard reports from logs/learning_guards.jsonl."""
    return read_jsonl("learning_guards", limit=limit)
