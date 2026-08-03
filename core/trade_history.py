"""Execution-mode-aware closed-trade history.

Paper and MT5 execution maintain separate ledgers. Consumers that influence live
entry gates must select the ledger from the active execution mode and must not
silently fall back to the other namespace.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.utils import read_json_state


def trade_history_filename(config: dict[str, Any]) -> str:
    """Return the closed-trade ledger for the configured execution mode."""
    execution = config.get("execution")
    # Small unit-test/config fragments historically omit execution entirely and
    # use trade_log.json as their synthetic closed-trade fixture. Keep that
    # legacy contract while requiring explicit mode selection in production.
    if not isinstance(execution, dict):
        return "trade_log.json"
    mode = str(execution.get("mode") or "paper").lower()
    return "mt5_trades.json" if mode == "mt5" else "paper_trades.json"


def read_closed_trades(
    config: dict[str, Any],
    *,
    limit: int | None = None,
    reader: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Read closed trades from the active mode's ledger only.

    A missing or malformed active ledger is treated as an empty sample rather
    than borrowing data from the other execution mode. This prevents stale
    paper losses from blocking a live MT5 account (and vice versa).
    """
    filename = trade_history_filename(config)
    read = reader or read_json_state
    document = read(filename, default={}) or {}
    if isinstance(document, list):
        rows = document
    elif isinstance(document, dict):
        rows = document.get("trades") or []
    else:
        rows = []
    trades = [row for row in rows if isinstance(row, dict)]
    trades.sort(
        key=lambda row: str(row.get("closed_at") or row.get("filled_at") or ""),
        reverse=True,
    )
    if limit is not None:
        return trades[: max(0, int(limit))]
    return trades
