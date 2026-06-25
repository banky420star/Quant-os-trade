"""Session scoring — favor high-liquidity, technically reliable trading windows (UTC)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# UTC hour ranges (start inclusive, end exclusive). Checked in priority order.
SESSION_WINDOWS: tuple[tuple[str, int, int], ...] = (
    ("overlap_london_ny", 12, 16),   # 14:00-18:00 SAST — best for XAU
    ("london_open", 7, 10),          # 09:00-12:00 SAST
    ("london_mid", 10, 12),          # 12:00-14:00 SAST
    ("new_york", 16, 20),            # 18:00-22:00 SAST — NY afternoon
    ("tokyo", 0, 9),                 # 02:00-11:00 SAST
    ("sydney", 22, 24),              # wraps via second row below
)

DEFAULT_SESSION_SCORES: dict[str, int] = {
    "london_open": 10,
    "overlap_london_ny": 10,
    "london_mid": 8,
    "new_york": 6,
    "tokyo": 4,
    "sydney": 2,
    "off_hours": 1,
    "rollover": 0,
}

# Extra session points when symbol aligns with its best liquidity windows.
SYMBOL_SESSION_BOOST: dict[str, dict[str, int]] = {
    "XAUUSDm": {"london_open": 2, "overlap_london_ny": 2, "london_mid": 1},
    "BTCUSDm": {"london_open": 1, "overlap_london_ny": 2, "new_york": 1},
    "USOILm": {"new_york": 2, "overlap_london_ny": 1},
}


def utc_hour(now: datetime | None = None) -> int:
    dt = now or datetime.now(timezone.utc)
    return dt.hour


def is_rollover_hour(hour: int) -> bool:
    """Avoid broker rollover (~22:00 UTC / 00:00 SAST)."""
    return hour >= 21 or hour < 1


def resolve_trading_session(hour: int | None = None) -> str:
    """Return the highest-priority session label for a UTC hour."""
    h = utc_hour() if hour is None else hour

    if is_rollover_hour(h):
        return "rollover"

    for name, start, end in SESSION_WINDOWS:
        if start <= h < end:
            return name

    # Sydney session wraps midnight: 22:00-24:00 and 00:00-07:00 (after rollover check)
    if h >= 22 or h < 7:
        return "sydney"

    return "off_hours"


def session_score(
    session: str,
    symbol: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Raw session score on 0-10 scale (capped at 10).

    Returns score payload used by trade_score and explain reports.
    """
    cfg = (config or {}).get("session_scoring", {})
    scores = {**DEFAULT_SESSION_SCORES, **cfg.get("session_scores", {})}
    boosts = {**SYMBOL_SESSION_BOOST, **cfg.get("symbol_session_boost", {})}

    raw = int(scores.get(session, scores.get("off_hours", 1)))
    boost = int(boosts.get(symbol, {}).get(session, 0))
    final = min(10, raw + boost)
    normalized = round(final / 10 * 100, 1)

    quality = (
        "excellent" if final >= 10
        else "very_good" if final >= 8
        else "good" if final >= 6
        else "medium" if final >= 4
        else "low"
    )

    return {
        "session": session,
        "raw_score": final,
        "max_raw": 10,
        "normalized": normalized,
        "quality": quality,
        "boost": boost,
        "rollover": session == "rollover",
    }