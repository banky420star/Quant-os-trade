"""Daily realized P&L aggregator — used by the $400/day halt gate and dashboard tile.

The day boundary is UTC midnight (matches the bot's running UTC clock — the bot
operates across sessions but the day boundary that matters for a "we hit $400"
event is universally consistent across the user's various session times).

Only CLOSED trades (those with a ``closed_at`` timestamp set) contribute. Open
positions do not, even if their floating PnL already exceeds the target.

Defensive behavior:
  - missing closed_at → skipped (no implicit "today")
  - non-numeric pnl → skipped, NOT thrown
  - empty / missing paper_trades.json → returns 0.0 (halt not triggered)
  - parses ISO 8601 with / without trailing 'Z'
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.utils import read_json_state

_PROJ = Path(__file__).resolve().parent.parent


def today_realized_pnl_usd(state_path: str = "paper_trades.json", since_iso: str | None = None) -> float:
    """Sum of closed trade pnl from today's UTC midnight onward. Returns 0.0 on
    empty/missing state. Used by the daily-profit-halt gate.

    ``since_iso`` (optional, dashboard session-reset): when provided, only
    trades closed AFTER this timestamp count. The halt gate in
    execution_loop calls WITHOUT this arg so the guardrail keeps counting
    the real full-day total; only the dashboard display honors a reset.
    """
    cutoff = _today_utc_midnight()
    since_dt = _parse_iso(since_iso) if since_iso else None
    doc = read_json_state(state_path, default={}) or {}
    trades: list[Any] = []
    if isinstance(doc, dict):
        raw = doc.get("trades") or []
        if isinstance(raw, list):
            trades = raw
    total = 0.0
    for entry in trades:
        if not isinstance(entry, dict):
            continue
        ts_raw = entry.get("closed_at") or entry.get("close_ts") or ""
        if not ts_raw:
            continue
        try:
            closed_dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            if closed_dt.tzinfo is None:
                closed_dt = closed_dt.replace(tzinfo=timezone.utc)
            if closed_dt < cutoff:
                continue
            if since_dt is not None and closed_dt < since_dt:
                continue
        except (ValueError, TypeError):
            continue
        pnl = entry.get("pnl")
        if pnl is None:
            continue
        try:
            total += float(pnl)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def today_realized_pnl_breakdown(state_path: str = "paper_trades.json", since_iso: str | None = None) -> dict[str, Any]:
    """Symbol-level breakdown of today's realized PnL for the dashboard tile.

    ``since_iso`` (optional): when provided, only trades closed AFTER this
    timestamp count (dashboard session-reset view). The halt gate calls
    without it.

    Returns
    -------
        {
          "by_symbol": <dict[str, float]>,
          "wins": <int>,
          "losses": <int>,
          "closed_count": <int>,
          "since_utc": <iso string>,
          "total_usd": <float>,
        }
    """
    cutoff = _today_utc_midnight()
    since_dt = _parse_iso(since_iso) if since_iso else None
    doc = read_json_state(state_path, default={}) or {}
    trades: list[Any] = []
    if isinstance(doc, dict):
        raw = doc.get("trades") or []
        if isinstance(raw, list):
            trades = raw
    per_symbol_detail: dict[str, dict[str, int | float]] = {}
    by_symbol: dict[str, list[float]] = {}
    wins = losses = 0
    for entry in trades:
        if not isinstance(entry, dict):
            continue
        ts_raw = entry.get("closed_at") or entry.get("close_ts") or ""
        if not ts_raw:
            continue
        try:
            closed_dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            if closed_dt.tzinfo is None:
                closed_dt = closed_dt.replace(tzinfo=timezone.utc)
            if closed_dt < cutoff:
                continue
            if since_dt is not None and closed_dt < since_dt:
                continue
        except (ValueError, TypeError):
            continue
        sym = str(entry.get("symbol") or "?")
        try:
            pnl = float(entry.get("pnl", 0) or 0)
        except (TypeError, ValueError):
            continue
        by_symbol.setdefault(sym, []).append(pnl)
        bucket = per_symbol_detail.setdefault(
            sym, {"pnl": 0.0, "closed_count": 0, "wins": 0, "losses": 0}
        )
        bucket["pnl"] = round(bucket["pnl"] + pnl, 2)
        bucket["closed_count"] += 1
        if pnl > 0:
            wins += 1
            bucket["wins"] += 1
        elif pnl < 0:
            losses += 1
            bucket["losses"] += 1
    return {
        "by_symbol": {k: round(sum(v), 2) for k, v in by_symbol.items()},
        "per_symbol_detail": {
            k: {
                "pnl": v["pnl"],
                "closed_count": v["closed_count"],
                "wins": v["wins"],
                "losses": v["losses"],
            }
            for k, v in per_symbol_detail.items()
        },
        "wins": wins,
        "losses": losses,
        "closed_count": wins + losses,
        "since_utc": cutoff.isoformat(),
        "total_usd": round(sum(sum(v) for v in by_symbol.values()), 2),
    }


def _today_utc_midnight() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO 8601 timestamp to an aware UTC datetime, or None on failure."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def today_utc_day_stamp() -> str:
    """Return today's UTC date as YYYY-MM-DD (the halt gate's day-stamp key)."""
    return _today_utc_midnight().strftime("%Y-%m-%d")
