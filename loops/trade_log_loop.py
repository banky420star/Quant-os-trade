"""Agent: comprehensive per-trade log — the user's "whole works" trade record.

USER feature request 2026-07-01: a clean trade log holding every field per trade
(opened/closed times, won/lost, setup, drawdown, entry/exit/SL/TP, side, size,
pnl, R-multiple, confidence, regime/session/bias, hold time, exit reason).

This loop is a thin, throttled wrapper around scripts.build_trade_log.build_log.
It rebuilds state/trade_log.json only when state/paper_trades.json changed (a
trade closed) -- so it does NOT hammer MT5 with 283 copy_rates_range calls on
every pipeline cycle. Reuses the bot's already-initialized in-process MT5
connection (no separate connect/shutdown). Read-only on MT5.

See scripts/build_trade_log.py for the join + drawdown + R-multiple logic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import load_config, read_json_state, setup_logger, write_json_state  # noqa: E402

# build_log uses the module-level MetaTrader5 handle directly (in-process), so
# it works inside the bot process without a fresh connect.
from scripts.build_trade_log import build_log  # noqa: E402

_STATE = Path(__file__).resolve().parent.parent / "state"
_PAPER_TRADES = _STATE / "paper_trades.json"
_PAPER_ORDERS = _STATE / "paper_orders.json"


def _source_mtime() -> tuple[float, float]:
    trades_m = orders_m = 0.0
    try:
        trades_m = os.path.getmtime(_PAPER_TRADES)
    except OSError:
        pass
    try:
        orders_m = os.path.getmtime(_PAPER_ORDERS)
    except OSError:
        pass
    return trades_m, orders_m


def run() -> dict:
    config = load_config()
    logger = setup_logger("trade_log_loop", "trade_log_loop.log")

    trades_m, orders_m = _source_mtime()
    prev = read_json_state("trade_log.json", default={}) or {}
    last_trades_m = float(prev.get("_paper_trades_mtime", 0.0) or 0.0)
    last_orders_m = float(prev.get("_paper_orders_mtime", 0.0) or 0.0)

    # Rebuild when a trade closed OR a new order filled (richer signal_meta).
    if prev and trades_m <= last_trades_m and orders_m <= last_orders_m:
        return prev

    try:
        payload = build_log(config, days=30, log=logger)
        payload["_paper_trades_mtime"] = trades_m
        payload["_paper_orders_mtime"] = orders_m
        write_json_state("trade_log.json", payload)
        logger.info(
            "Trade log rebuilt: %d trades (paper_trades mtime %.0f, orders mtime %.0f).",
            payload.get("total", 0),
            trades_m,
            orders_m,
        )
        return payload
    except Exception as exc:  # noqa: BLE001 -- fault-isolated from the pipeline
        logger.error("Trade log rebuild failed: %s", exc)
        return prev or {}