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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import load_config, read_json_state, setup_logger, write_json_state  # noqa: E402

# build_log uses the module-level MetaTrader5 handle directly (in-process), so
# it works inside the bot process without a fresh connect.
from scripts.build_trade_log import build_log  # noqa: E402

_STATE = Path(__file__).resolve().parent.parent / "state"


def _active_ledger_path(config: dict) -> Path:
    """Closed-trade ledger for the configured execution mode (mt5_trades.json
    in MT5 mode, paper_trades.json in paper mode). See core/trade_history.py."""
    from core.trade_history import trade_history_filename
    return _STATE / trade_history_filename(config)


def _active_orders_path(config: dict) -> Path:
    """Order ledger for the configured execution mode (mt5_orders.json in MT5
    mode, paper_orders.json in paper mode). See core/trade_history.py."""
    from core.trade_history import trade_orders_filename
    return _STATE / trade_orders_filename(config)


def _source_mtime(config: dict) -> tuple[float, float]:
    trades_m = orders_m = 0.0
    try:
        trades_m = os.path.getmtime(_active_ledger_path(config))
    except OSError:
        pass
    try:
        orders_m = os.path.getmtime(_active_orders_path(config))
    except OSError:
        pass
    return trades_m, orders_m


def run() -> dict:
    config = load_config()
    logger = setup_logger("trade_log_loop", "trade_log_loop.log")

    trades_m, orders_m = _source_mtime(config)
    # Mtimes live in a separate state/trade_log_meta.json (always DICT).
    # trade_log.json itself is now a root-level LIST written by build_log,
    # so we cannot rely on prev's _paper_*_mtime keys (they're absent).
    prev_meta = read_json_state("trade_log_meta.json", default={}) or {}
    # ledger_mtime holds the active ledger's mtime (mt5_trades or paper_trades).
    # Fall back to the legacy paper_trades_mtime key for continuity.
    last_trades_m = float(
        prev_meta.get("ledger_mtime", prev_meta.get("paper_trades_mtime", 0.0)) or 0.0
    )
    # Key is "orders_mtime" (holds whichever mode's orders file — mt5_orders or
    # paper_orders). Fall back to the legacy paper_orders_mtime key for
    # continuity with meta files written before the mode-aware switch.
    last_orders_m = float(prev_meta.get("orders_mtime", prev_meta.get("paper_orders_mtime", 0.0)) or 0.0)

    # Rebuild when a trade closed OR a new order filled (richer signal_meta).
    if trades_m <= last_trades_m and orders_m <= last_orders_m:
        # Source files unchanged since the last rebuild — return cached.
        cached = read_json_state("trade_log.json", default=[]) or []
        return cached

    try:
        payload = build_log(config, days=30, log=logger)
        write_json_state("trade_log.json", payload)
        # Side-car: mtimes persisted in a separate DICT file so the LIST
        # shape of trade_log.json doesn't collapse the rebuild-skip check.
        write_json_state("trade_log_meta.json", {
            "ledger_mtime": trades_m,
            "orders_mtime": orders_m,
            # datetime.now(timezone.utc) is the modern replacement for the
            # deprecated utcnow(). The trailing .replace("+00:00", "Z")
            # normalises to the same "Z"-suffixed UTC string the rest of
            # the bot emits.
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
        n = len(payload) if isinstance(payload, list) else payload.get("total", 0)
        logger.info(
            "Trade log rebuilt: %d trades (ledger mtime %.0f, orders mtime %.0f).",
            n,
            trades_m,
            orders_m,
        )
        return payload
    except Exception as exc:  # noqa: BLE001 -- fault-isolated from the pipeline
        logger.error("Trade log rebuild failed: %s", exc)
        return read_json_state("trade_log.json", default=[]) or []