"""Sync open positions from MT5 for verifier and risk loops."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from core.symbol_manager import logical_symbol

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore

_COMMENT_SETUP_RE = re.compile(r"^qagent_(.+)$")


def _setup_type_from_comment(comment: str) -> str:
    if not comment:
        return "unknown"
    match = _COMMENT_SETUP_RE.match(comment.strip())
    return match.group(1) if match else comment.strip() or "unknown"


def fetch_mt5_agent_positions(
    config: dict[str, Any],
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """Return open MT5 positions placed by this agent (filtered by magic number)."""
    if mt5 is None:
        raise RuntimeError("MetaTrader5 package not installed")

    magic = int(config.get("execution", {}).get("magic_number", 20250625))
    positions = mt5.positions_get()
    if not positions:
        return []

    synced: list[dict[str, Any]] = []
    for pos in positions:
        if pos.magic != magic:
            continue
        setup_type = _setup_type_from_comment(pos.comment)
        synced.append({
            "position_id": str(pos.ticket),
            "ticket": pos.ticket,
            "symbol": logical_symbol(pos.symbol),
            "broker_symbol": pos.symbol,
            "side": "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL",
            "entry": float(pos.price_open),
            "sl": float(pos.sl),
            "tp1": float(pos.tp),
            "size": float(pos.volume),
            "profit": float(pos.profit),
            "opened_at": datetime.fromtimestamp(int(pos.time), tz=timezone.utc).isoformat(),
            "setup_type": setup_type,
            "magic": pos.magic,
            "comment": pos.comment,
            "source": "mt5",
        })

    if logger:
        logger.info("MT5 position sync: %d agent positions (magic=%s)", len(synced), magic)
    return synced