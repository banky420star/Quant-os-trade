"""News calendar — scheduled blackout + macro event awareness for live and replay.

Aspect A: scheduled US release blackout (verifier gate).
Aspect B: macro event context (NFP / FOMC / CPI) for informed trading decisions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

FOMC_DATES = [
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31",
    "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30",
    "2025-09-17", "2025-10-29", "2025-12-17",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
]


def news_settings(config: dict[str, Any]) -> dict[str, Any]:
    filters = config.get("filters") or {}
    news = config.get("news") or {}
    return {
        "avoid_news": bool(filters.get("avoid_news", news.get("avoid_news", False))),
        "blackout_minutes": float(
            filters.get("news_blackout_minutes", news.get("blackout_minutes", 10))
        ),
        "release_times_utc": list(
            filters.get("news_release_times_utc", news.get("release_times_utc", [])) or []
        ),
        "macro_awareness": bool(news.get("macro_awareness", True)),
        "macro_blackout_minutes": float(news.get("macro_blackout_minutes", 15)),
        "sentiment_gate": bool(news.get("sentiment_gate", False)),
        "min_sentiment_confidence": float(news.get("min_sentiment_confidence", 0.55)),
    }


def _to_utc_datetime(when: datetime | str | Any | None) -> datetime:
    if when is None:
        return datetime.now(timezone.utc)
    if isinstance(when, datetime):
        return when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)
    try:
        import pandas as pd
        ts = pd.Timestamp(when)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.to_pydatetime().astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _minute_distance(now_min: float, rel_min: float) -> float:
    dist = abs(now_min - rel_min)
    return min(dist, 1440.0 - dist)


def is_scheduled_blackout(
    when: datetime | str | Any | None,
    *,
    release_times_utc: list[str],
    window_minutes: float,
) -> bool:
    """True when *when* falls within +/- window of a scheduled release slot."""
    if not release_times_utc or window_minutes <= 0:
        return False
    dt = _to_utc_datetime(when)
    now_min = dt.hour * 60 + dt.minute + dt.second / 60.0
    for t in release_times_utc:
        try:
            hh, mm = str(t).split(":")
            rel = int(hh) * 60 + int(mm)
        except (ValueError, AttributeError):
            continue
        if _minute_distance(now_min, float(rel)) <= window_minutes:
            return True
    return False


def detect_macro_events(when: datetime | str | Any | None) -> list[dict[str, Any]]:
    """Detect constructed macro calendar events at *when* (UTC)."""
    dt = _to_utc_datetime(when)
    day = dt.strftime("%Y-%m-%d")
    hour = dt.hour
    events: list[dict[str, Any]] = []

    if dt.weekday() == 4 and dt.day <= 7 and 11 <= hour <= 14:
        events.append({
            "type": "nfp",
            "theme": "employment",
            "label": "NFP release window",
            "sentiment_hint": "volatile",
            "impact": "high",
        })
    if day in FOMC_DATES and 17 <= hour <= 20:
        events.append({
            "type": "fomc",
            "theme": "inflation",
            "label": "FOMC announcement window",
            "sentiment_hint": "volatile",
            "impact": "high",
        })
    if dt.weekday() in (1, 2) and 8 <= dt.day <= 14 and 11 <= hour <= 14:
        events.append({
            "type": "cpi",
            "theme": "inflation",
            "label": "CPI release window",
            "sentiment_hint": "volatile",
            "impact": "high",
        })
    return events


def news_context_at(
    when: datetime | str | Any | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Full news context for live or historical bar time."""
    cfg = news_settings(config)
    dt = _to_utc_datetime(when)
    scheduled_blackout = (
        cfg["avoid_news"]
        and is_scheduled_blackout(
            dt,
            release_times_utc=cfg["release_times_utc"],
            window_minutes=cfg["blackout_minutes"],
        )
    )
    macro_events: list[dict[str, Any]] = []
    macro_blackout = False
    if cfg["macro_awareness"]:
        macro_events = detect_macro_events(dt)
        if macro_events:
            macro_blackout = is_scheduled_blackout(
                dt,
                release_times_utc=cfg["release_times_utc"] or ["12:30", "13:30", "14:00", "14:30"],
                window_minutes=cfg["macro_blackout_minutes"],
            )

    sentiment = "neutral"
    if macro_events:
        sentiment = macro_events[0].get("sentiment_hint", "volatile")

    safe_for_entry = not scheduled_blackout and not macro_blackout
    return {
        "timestamp": dt.isoformat(),
        "scheduled_blackout": scheduled_blackout,
        "macro_blackout": macro_blackout,
        "macro_events": macro_events,
        "sentiment_hint": sentiment,
        "safe_for_entry": safe_for_entry,
        "aspect_b_active": bool(macro_events),
    }


def entry_allowed_by_news(
    when: datetime | str | Any | None,
    config: dict[str, Any],
) -> bool:
    """Verifier-compatible: True when news gates allow a new entry."""
    return bool(news_context_at(when, config).get("safe_for_entry", True))