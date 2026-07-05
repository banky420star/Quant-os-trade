"""Tests for MT5Broker batch execution respecting Blue Guardian caps."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.mt5_broker import MT5Broker


def _bg_config():
    return {
        "blue_guardian": {
            "enabled": True,
            "max_total_open_positions": 4,
            "max_open_per_symbol": 1,
            "account_size_usd": 5000,
            "risk_per_trade_usd": 25,
        },
        "execution": {
            "mode": "mt5",
            "mt5_trading_enabled": True,
            "allow_live_account": True,
            "magic_number": 20250625,
            "default_lot": 0.01,
            "max_lot": 0.1,
        },
        "mt5": {"account_mode": "real"},
        "signals": {"default_risk_percent": 0.5},
        "risk": {"max_symbol_exposure_usd": 5000, "max_total_exposure_usd": 5000},
        "trading": {"max_open_per_symbol": 1, "allow_pyramiding": False},
    }


def _signal(idx: int, symbol: str) -> dict:
    return {
        "signal": {
            "signal_id": f"sig-{idx}",
            "symbol": symbol,
            "side": "BUY",
            "entry": 100.0,
            "sl": 99.0,
            "tp1": 102.0,
            "setup_type": "pullback",
            "confidence": 90,
        }
    }


@pytest.fixture
def mock_mt5(monkeypatch):
    mt5 = MagicMock()
    account = MagicMock(login=123456789, server="Demo-Server", balance=5000.0, equity=5000.0, trade_mode=2, trade_allowed=True)
    terminal = MagicMock(trade_allowed=True)
    mt5.account_info.return_value = account
    mt5.terminal_info.return_value = terminal
    mt5.positions_get.return_value = []
    monkeypatch.setattr("core.mt5_broker.mt5", mt5)
    return mt5


def test_batch_stops_at_max_total_open_positions(mock_mt5, monkeypatch):
    broker = MT5Broker(_bg_config())
    symbols = ["US30m", "US500m", "UK100m", "FR40m", "NAS100m", "XAUUSDm", "USOILm"]
    approved = [_signal(i, sym) for i, sym in enumerate(symbols)]

    placed = {"count": 0}

    def fake_place(signal, account, open_positions=None):
        placed["count"] += 1
        return {"success": True, "ticket": placed["count"], "volume": 0.01, "price": 100.0}

    def fake_sync():
        return [
            {
                "ticket": i,
                "symbol": symbols[i - 1],
                "side": "BUY",
                "size": 0.01,
                "profit": 0.0,
                "magic": 20250625,
            }
            for i in range(1, placed["count"] + 1)
        ]

    monkeypatch.setattr(broker, "_place_order", fake_place)
    monkeypatch.setattr(broker, "_sync_positions", fake_sync)
    monkeypatch.setattr(broker, "_validate_account_mode", lambda _account: None)

    result = broker.process_approved_signals(approved)
    assert len(result["placed"]) == 4
    assert placed["count"] == 4