"""Dashboard safety freshness must follow the market feed, not slow account snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from dashboard.safety import build_safety_header


def _config() -> dict:
    return {
        "execution": {
            "live_trading_enabled": True,
            "explicit_opt_in_danger_zone": True,
        },
        "mt5": {"account_mode": "demo"},
        "fast_mode": {"enabled": False, "live_enabled": False},
        "adaptation": {"enabled": False},
        "learning": {"mode": "observe_only"},
    }


def _stale_iso(minutes: int = 20) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _fresh_iso(seconds: int = 5) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_healthy_market_feed_overrides_stale_account_snapshot():
    account = {"account_mode": "demo", "timestamp": _stale_iso()}
    portfolio = {"updated_at": _stale_iso(), "open_positions": []}
    feed = {
        "worker_alive": True,
        "refresh_failures_consecutive": 0,
        "last_market_timestamp": _fresh_iso(),
    }

    with patch("core.utils.read_json_state", return_value=feed):
        result = build_safety_header(
            account,
            _config(),
            portfolio,
            {"status": "healthy"},
            {"kill_switch": False},
        )

    assert result["execution_state"] == "ARMED"
    assert result["data_freshness"] in {"fresh", "warm"}
    assert result["data_freshness_source"] == "market_data_feed"
    assert "data freshness is stale" not in result["execution_blockers"]


def test_stale_market_timestamp_remains_fail_closed():
    stale = _stale_iso()
    feed = {
        "worker_alive": True,
        "refresh_failures_consecutive": 0,
        "last_market_timestamp": stale,
    }

    with patch("core.utils.read_json_state", return_value=feed):
        result = build_safety_header(
            {"account_mode": "demo", "timestamp": stale},
            _config(),
            {"updated_at": stale, "open_positions": []},
            {"status": "healthy"},
            {"kill_switch": False},
        )

    assert result["execution_state"] == "DISARMED"
    assert result["data_freshness"] == "stale"
    assert "data freshness is stale" in result["execution_blockers"]


def test_unhealthy_feed_cannot_make_stale_snapshot_fresh():
    stale = _stale_iso()
    feed = {
        "worker_alive": False,
        "refresh_failures_consecutive": 4,
        "last_market_timestamp": _fresh_iso(),
    }

    with patch("core.utils.read_json_state", return_value=feed):
        result = build_safety_header(
            {"account_mode": "demo", "timestamp": stale},
            _config(),
            {"updated_at": stale, "open_positions": []},
            {"status": "healthy"},
            {"kill_switch": False},
        )

    assert result["execution_state"] == "DISARMED"
    assert result["data_freshness_source"] != "market_data_feed"
