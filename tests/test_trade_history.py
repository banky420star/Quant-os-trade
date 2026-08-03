"""Execution-mode trade-history isolation tests."""

from __future__ import annotations

from core.trade_history import read_closed_trades, trade_history_filename


def test_mt5_mode_reads_only_mt5_ledger():
    calls: list[str] = []

    def read(name, default=None):
        calls.append(name)
        if name == "mt5_trades.json":
            return {"trades": [{"symbol": "EURUSDm", "pnl": 1.0, "closed_at": "2026-07-31T12:00:00Z"}]}
        if name == "paper_trades.json":
            return {"trades": [{"symbol": "NAS100m", "pnl": -999.0, "closed_at": "2026-07-31T13:00:00Z"}]}
        return default

    config = {"execution": {"mode": "mt5"}}
    assert trade_history_filename(config) == "mt5_trades.json"
    rows = read_closed_trades(config, reader=read)

    assert [row["symbol"] for row in rows] == ["EURUSDm"]
    assert calls == ["mt5_trades.json"]


def test_paper_mode_reads_only_paper_ledger():
    calls: list[str] = []

    def read(name, default=None):
        calls.append(name)
        if name == "paper_trades.json":
            return {"trades": [{"symbol": "XAUUSDm", "pnl": 0.5, "closed_at": "2026-07-31T12:00:00Z"}]}
        return default

    config = {"execution": {"mode": "paper"}}
    assert trade_history_filename(config) == "paper_trades.json"
    rows = read_closed_trades(config, reader=read)

    assert [row["symbol"] for row in rows] == ["XAUUSDm"]
    assert calls == ["paper_trades.json"]


def test_missing_execution_keeps_fixture_compatibility():
    assert trade_history_filename({}) == "trade_log.json"
