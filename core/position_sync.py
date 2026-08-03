"""Sync open positions from MT5 for verifier and risk loops."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from core.symbol_manager import logical_symbol
from core.utils import read_json_state

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore

_COMMENT_SETUP_RE = re.compile(r"^qagent_(.+)$")


def _setup_type_from_comment(comment: str) -> str:
    if not comment:
        return "unknown"
    raw = comment.strip()
    match = _COMMENT_SETUP_RE.match(raw)
    extracted = match.group(1) if match else raw
    # 2026-07-31 — route through normalize_setup_type so broker-truncated
    # setup names (qagent_donchian_ → donchian_ → donchian_breakout) are
    # repaired at the source. Deferred import avoids a core.strategy_policy
    # import cycle. Falls back to the raw extracted label if anything breaks.
    try:
        from core.strategy_policy import normalize_setup_type
        return normalize_setup_type(extracted) or "unknown"
    except Exception:
        return extracted or "unknown"


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

    open_times = read_json_state("position_open_times.json", default={}) or {}

    synced: list[dict[str, Any]] = []
    for pos in positions:
        if pos.magic != magic:
            continue
        setup_type = _setup_type_from_comment(pos.comment)
        ticket = str(pos.ticket)
        opened_at = open_times.get(ticket)
        if not opened_at:
            opened_at = datetime.fromtimestamp(int(pos.time), tz=timezone.utc).isoformat()
        synced.append({
            "position_id": ticket,
            "ticket": pos.ticket,
            "symbol": logical_symbol(pos.symbol),
            "broker_symbol": pos.symbol,
            "side": "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL",
            "entry": float(pos.price_open),
            "sl": float(pos.sl),
            "tp1": float(pos.tp),
            "size": float(pos.volume),
            "profit": float(pos.profit),
            "opened_at": opened_at,
            "setup_type": setup_type,
            "magic": pos.magic,
            "comment": pos.comment,
            "source": "mt5",
        })

    if logger:
        logger.info("MT5 position sync: %d agent positions (magic=%s)", len(synced), magic)
    return synced