"""Trade quantity limits — config helpers."""

from __future__ import annotations

from typing import Any

from core.dynamic_entry import symbol_capacity_available as _symbol_capacity_available
from core.dynamic_entry import symbol_position_count


def unlimited_trades(config: dict[str, Any]) -> bool:
    """When true, no cap on candidates, exposure, or duplicate positions."""
    return bool(config.get("risk", {}).get("unlimited_trades", False))


def allow_pyramiding(config: dict[str, Any]) -> bool:
    """When true, stack positions from different signals (same symbol/side allowed)."""
    return bool(config.get("trading", {}).get("allow_pyramiding", False))


def enrich_positions_with_orders(
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach signal_id/setup_type from filled orders onto live positions."""
    by_ticket: dict[int, dict[str, Any]] = {}
    for order in orders:
        ticket = order.get("mt5_ticket")
        if ticket is None or order.get("status") != "filled":
            continue
        by_ticket[int(ticket)] = order

    enriched: list[dict[str, Any]] = []
    for pos in positions:
        row = dict(pos)
        ticket = pos.get("ticket")
        if ticket is not None:
            order = by_ticket.get(int(ticket))
            if order:
                row.setdefault("signal_id", order.get("signal_id"))
                row.setdefault("setup_type", order.get("setup_type"))
        enriched.append(row)
    return enriched


def symbol_capacity_available(
    config: dict[str, Any],
    symbol: str,
    active_positions: list[dict[str, Any]],
) -> bool:
    """True when symbol is below max_open_per_symbol limit."""
    return _symbol_capacity_available(config, symbol, active_positions)


def is_duplicate_position(
    config: dict[str, Any],
    signal: dict[str, Any],
    active_positions: list[dict[str, Any]],
    *,
    executed_signal_ids: set[str] | None = None,
) -> bool:
    """
    Return True if this signal should not open another position.

    Pyramiding: allow same symbol/side when signal differs; block re-entry of
    the same signal_id. Optional pyramid_block_same_setup blocks identical setups.
    """
    sid = signal.get("signal_id")
    if sid and executed_signal_ids and sid in executed_signal_ids:
        return True

    symbol = signal.get("symbol")
    side = signal.get("side")
    setup = signal.get("setup_type")

    if unlimited_trades(config) and not allow_pyramiding(config):
        return False

    if not symbol_capacity_available(config, symbol, active_positions):
        return True

    if allow_pyramiding(config) or unlimited_trades(config):
        block_same_setup = bool(config.get("trading", {}).get("pyramid_block_same_setup", False))
        for active in active_positions:
            if sid and active.get("signal_id") == sid:
                return True
            if not block_same_setup:
                continue
            if (
                active.get("symbol") == symbol
                and active.get("side") == side
                and active.get("setup_type") == setup
            ):
                return True
        return False

    mode = config.get("execution", {}).get("mode", "paper")
    for active in active_positions:
        if active.get("symbol") != symbol or active.get("side") != side:
            continue
        if mode == "mt5":
            return True
        if active.get("setup_type") == setup:
            return True
    return False


def max_candidates_per_run(config: dict[str, Any]) -> int | None:
    """Return max candidates, or None for unlimited (0 or unlimited_trades)."""
    if unlimited_trades(config):
        return None
    n = int(config.get("signals", {}).get("max_candidates_per_run", 10))
    return None if n <= 0 else n