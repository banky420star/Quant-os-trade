from __future__ import annotations

from datetime import datetime, timedelta, timezone

import core.rvi_exit as rvi_exit


def _bar(ts: datetime, o: float = 100.0, h: float = 101.0, l: float = 99.0, c: float = 100.5) -> dict:
    return {
        "time": ts.isoformat(),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
    }


def test_confirmed_cross_closes_buy_on_bearish_cross(monkeypatch):
    monkeypatch.setattr(rvi_exit, "rvi_series", lambda bars, period=10: ([0.2, -0.1], [0.1, 0.05]))

    crossed, reason, values = rvi_exit.confirmed_cross("BUY", [{}] * 20, period=10)

    assert crossed is True
    assert reason == "rvi_m15_bearish_cross"
    assert values["prev_main"] >= values["prev_signal"]
    assert values["main"] < values["signal"]


def test_confirmed_cross_closes_sell_on_bullish_cross(monkeypatch):
    monkeypatch.setattr(rvi_exit, "rvi_series", lambda bars, period=10: ([-0.2, 0.1], [-0.1, -0.05]))

    crossed, reason, values = rvi_exit.confirmed_cross("SELL", [{}] * 20, period=10)

    assert crossed is True
    assert reason == "rvi_m15_bullish_cross"
    assert values["prev_main"] <= values["prev_signal"]
    assert values["main"] > values["signal"]


def test_rvi_exit_requires_fresh_completed_m15_data(monkeypatch):
    now = datetime.now(timezone.utc)
    bars = [_bar(now - timedelta(minutes=15 * (25 - i))) for i in range(25)]
    snapshot = {"symbols": {"XAUUSDm": {"M15": bars}}}
    config = {
        "trading": {
            "rvi_exit": {
                "enabled": True,
                "period": 10,
                "max_data_age_seconds": 1200,
                "per_symbol": {"XAUUSDm": {"period": 10}},
            }
        }
    }
    position = {"symbol": "XAUUSDm", "side": "BUY"}

    monkeypatch.setattr(rvi_exit, "confirmed_cross", lambda side, completed, period=10: (True, "rvi_m15_bearish_cross", {}))
    verdict = rvi_exit.evaluate_position_rvi_exit(config, position, snapshot, now=now)

    assert verdict["close"] is True
    assert verdict["reason"] == "rvi_m15_bearish_cross"


def test_rvi_exit_fails_closed_on_stale_m15_data(monkeypatch):
    now = datetime.now(timezone.utc)
    bars = [_bar(now - timedelta(hours=10, minutes=15 * (25 - i))) for i in range(25)]
    snapshot = {"symbols": {"BTCUSDm": {"M15": bars}}}
    config = {
        "trading": {
            "rvi_exit": {
                "enabled": True,
                "period": 10,
                "max_data_age_seconds": 1200,
                "per_symbol": {"BTCUSDm": {"period": 10}},
            }
        }
    }
    position = {"symbol": "BTCUSDm", "side": "SELL"}

    monkeypatch.setattr(rvi_exit, "confirmed_cross", lambda *args, **kwargs: (True, "rvi_m15_bullish_cross", {}))
    verdict = rvi_exit.evaluate_position_rvi_exit(config, position, snapshot, now=now)

    assert verdict["close"] is False
    assert verdict["reason"] == "stale_m15_data"


def test_rvi_exit_fails_closed_when_symbol_has_no_m15_bars():
    config = {"trading": {"rvi_exit": {"enabled": True, "period": 10}}}
    position = {"symbol": "EURUSDm", "side": "BUY"}

    verdict = rvi_exit.evaluate_position_rvi_exit(config, position, {"symbols": {}})

    assert verdict["close"] is False
    assert verdict["reason"] == "insufficient_m15_bars"
