"""Mode-aware order-ledger selection + live-trade session/hour recovery.

2026-08-04: in MT5 mode the placed orders (with the rich signal_meta) live in
mt5_orders.json, NOT paper_orders.json (which stays empty). Consumers that join
closed trades back to their opening order must select the orders ledger from the
same execution-mode switch — otherwise every live trade loses its
setup/session/hour attribution and per-session evaluation is empty.
"""

from __future__ import annotations

import pandas as pd

from core.trade_history import trade_orders_filename
from scripts import analyze_session_edge as ase


def test_orders_filename_matches_mode():
    assert trade_orders_filename({"execution": {"mode": "mt5"}}) == "mt5_orders.json"
    assert trade_orders_filename({"execution": {"mode": "paper"}}) == "paper_orders.json"
    assert trade_orders_filename({}) == "paper_orders.json"


def test_enrich_trades_recovers_session_and_hour_from_order():
    trades = [
        {"mt5_position": 111, "mt5_deal": 999, "closed_at": "2026-08-04T07:00:00+00:00",
         "pnl": 1.0, "result": "win"},
    ]
    orders = [
        {"status": "filled", "mt5_ticket": 111, "setup_type": "silver_bullet",
         "filled_at": "2026-08-04T14:10:00+00:00",
         "signal_meta": {"setup_type": "silver_bullet", "confidence": 70,
                         "session": "london_open"}},
    ]
    out = ase.enrich_trades(trades, orders)
    row = out[0]
    assert row["setup_type"] == "silver_bullet"
    assert row["session"] == "london_open"
    assert row["utc_hour"] == 14
    assert row["opened_at"] == "2026-08-04T14:10:00+00:00"


def test_enrich_trades_session_from_market_context_fallback():
    """Session nested under signal_meta.market_context also works."""
    trades = [{"mt5_position": 222, "closed_at": "2026-08-04T20:00:00+00:00", "pnl": -2.0}]
    orders = [{"status": "filled", "mt5_ticket": 222, "setup_type": "pullback",
               "created_at": "2026-08-04T08:00:00+00:00",
               "signal_meta": {"market_context": {"session": "london_open"}}}]
    row = ase.enrich_trades(trades, orders)[0]
    assert row["session"] == "london_open"
    assert row["utc_hour"] == 8


def test_enrich_trades_falls_back_to_close_hour_without_order():
    """Archive trades with no opening order still land in an hour bucket."""
    trades = [{"mt5_position": None, "closed_at": "2026-08-04T15:30:00+00:00",
               "pnl": 3.0, "result": "win"}]
    row = ase.enrich_trades(trades, [])[0]
    assert row["utc_hour"] == 15
    assert row["session"]  # resolved from close hour


def test_enrich_trades_no_order_no_timestamp_stays_unknown():
    trades = [{"closed_at": None, "pnl": 1.0}]
    row = ase.enrich_trades(trades, [])[0]
    assert row.get("utc_hour") is None
