"""Tests for news calendar — replay-time blackout + macro aspect B."""

from __future__ import annotations

from datetime import datetime, timezone

from core.news_calendar import (
    detect_macro_events,
    entry_allowed_by_news,
    is_scheduled_blackout,
    news_context_at,
)


def _cfg():
    return {
        "filters": {
            "avoid_news": True,
            "news_blackout_minutes": 10,
            "news_release_times_utc": ["14:30"],
        },
        "news": {"macro_awareness": True, "macro_blackout_minutes": 15},
    }


def test_scheduled_blackout_at_release_time():
    when = datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc)
    assert is_scheduled_blackout(when, release_times_utc=["14:30"], window_minutes=10) is True


def test_scheduled_blackout_outside_window():
    when = datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc)
    assert is_scheduled_blackout(when, release_times_utc=["14:30"], window_minutes=10) is False


def test_entry_allowed_uses_reference_time_not_live_clock():
    cfg = _cfg()
    safe_time = "2026-06-06T10:00:00+00:00"
    news_time = "2026-06-06T14:30:00+00:00"
    assert entry_allowed_by_news(safe_time, cfg) is True
    assert entry_allowed_by_news(news_time, cfg) is False


def test_macro_aspect_b_detects_nfp_window():
    # First Friday of June 2026 = June 5
    when = datetime(2026, 6, 5, 12, 30, tzinfo=timezone.utc)
    events = detect_macro_events(when)
    assert any(e["type"] == "nfp" for e in events)


def test_news_context_includes_aspect_b_flag():
    when = datetime(2026, 6, 5, 12, 30, tzinfo=timezone.utc)
    ctx = news_context_at(when, _cfg())
    assert ctx["aspect_b_active"] is True
    assert ctx["macro_events"]