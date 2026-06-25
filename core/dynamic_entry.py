"""Dynamic pyramid entries — spacing, positive stack PnL, per-symbol layers."""

from __future__ import annotations

import copy
from typing import Any


def _trading_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("trading", {})


def _dynamic_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return _trading_cfg(config).get("dynamic_entries", {})


def dynamic_entries_enabled(config: dict[str, Any]) -> bool:
    return bool(_dynamic_cfg(config).get("enabled", True))


def max_open_per_symbol(config: dict[str, Any]) -> int | None:
    n = int(_trading_cfg(config).get("max_open_per_symbol", 0))
    return n if n > 0 else None


def symbol_position_count(positions: list[dict[str, Any]], symbol: str) -> int:
    return sum(1 for p in positions if p.get("symbol") == symbol)


def symbol_capacity_available(
    config: dict[str, Any],
    symbol: str,
    positions: list[dict[str, Any]],
) -> bool:
    limit = max_open_per_symbol(config)
    if limit is None:
        return True
    return symbol_position_count(positions, symbol) < limit


def same_side_positions(
    positions: list[dict[str, Any]],
    symbol: str,
    side: str,
) -> list[dict[str, Any]]:
    return [
        p for p in positions
        if p.get("symbol") == symbol and p.get("side") == side
    ]


def symbol_same_side_unrealized(
    positions: list[dict[str, Any]],
    symbol: str,
    side: str,
    current_price: float,
) -> float:
    total = 0.0
    for pos in same_side_positions(positions, symbol, side):
        entry = float(pos.get("entry", current_price))
        size = float(pos.get("size", 1.0))
        diff = current_price - entry
        if side == "SELL":
            diff = -diff
        profit = pos.get("profit")
        if profit is not None:
            total += float(profit)
        else:
            total += diff * size
    return round(total, 2)


def pyramid_layer_index(
    signal: dict[str, Any],
    positions: list[dict[str, Any]],
) -> int:
    return len(same_side_positions(positions, signal["symbol"], signal["side"]))


def _symbol_dynamic_overrides(cfg: dict[str, Any], symbol: str) -> dict[str, Any]:
    per_symbol = cfg.get("per_symbol", {})
    return dict(per_symbol.get(symbol, {}))


def evaluate_dynamic_entry(
    config: dict[str, Any],
    signal: dict[str, Any],
    positions: list[dict[str, Any]],
    feat: dict[str, Any],
) -> tuple[bool, dict[str, Any], str | None]:
    """
    Validate and adjust a pyramid entry.

    Returns (allowed, adjusted_signal, failure_reason).
    """
    symbol = signal["symbol"]
    side = signal["side"]
    cfg = _dynamic_cfg(config)
    sym_cfg = _symbol_dynamic_overrides(cfg, symbol)

    if not dynamic_entries_enabled(config):
        return True, signal, None

    layer = pyramid_layer_index(signal, positions)
    if layer == 0:
        return True, refresh_entry_levels(signal, feat), None

    stack = same_side_positions(positions, symbol, side)
    price = float(feat.get("price", signal.get("entry", 0)))
    atr = float(feat.get("atr", price * 0.001) or price * 0.001)

    if bool(cfg.get("require_positive_stack_pnl", True)):
        stack_pnl = symbol_same_side_unrealized(positions, symbol, side, price)
        if stack_pnl <= 0:
            return False, signal, f"dynamic_entry:stack_not_positive:{stack_pnl}"

    min_dist_mult = float(sym_cfg.get("min_atr_distance_from_last", cfg.get("min_atr_distance_from_last", 0.35)))
    min_dist = atr * min_dist_mult
    last_entry = float(stack[-1].get("entry", price))
    moved = abs(price - last_entry)
    if moved < min_dist:
        return False, signal, f"dynamic_entry:too_close_to_last:{round(moved, 5)}<{round(min_dist, 5)}"

    if bool(cfg.get("require_favorable_price", True)):
        if side == "BUY" and price <= last_entry:
            return False, signal, "dynamic_entry:buy_not_above_last_entry"
        if side == "SELL" and price >= last_entry:
            return False, signal, "dynamic_entry:sell_not_below_last_entry"

    adjusted = refresh_entry_levels(signal, feat)
    adjusted["pyramid_layer"] = layer
    adjusted["dynamic_entry"] = {
        "layer": layer,
        "stack_pnl": symbol_same_side_unrealized(positions, symbol, side, price),
        "distance_from_last": round(moved, 5),
        "min_distance": round(min_dist, 5),
    }
    return True, adjusted, None


def refresh_entry_levels(signal: dict[str, Any], feat: dict[str, Any]) -> dict[str, Any]:
    """Re-anchor entry/SL/TP to live price while preserving risk distances."""
    out = copy.deepcopy(signal)
    price = float(feat.get("price", signal.get("entry", 0)))
    old_entry = float(signal.get("entry", price))
    old_sl = float(signal.get("sl", price))
    old_tp1 = float(signal.get("tp1", price))
    risk_dist = abs(old_entry - old_sl) or float(feat.get("atr", price * 0.001) or price * 0.001) * 1.5
    reward_dist = abs(old_tp1 - old_entry) or risk_dist * 1.5

    side = signal.get("side", "BUY")
    if side == "BUY":
        out["entry"] = round(price, 5)
        out["sl"] = round(price - risk_dist, 5)
        out["tp1"] = round(price + reward_dist, 5)
        if signal.get("tp2") is not None:
            out["tp2"] = round(price + reward_dist * 2, 5)
    else:
        out["entry"] = round(price, 5)
        out["sl"] = round(price + risk_dist, 5)
        out["tp1"] = round(price - reward_dist, 5)
        if signal.get("tp2") is not None:
            out["tp2"] = round(price - reward_dist * 2, 5)
    return out


def scale_lot_for_layer(config: dict[str, Any], symbol: str, base_lot: float, layer: int) -> float:
    cfg = _dynamic_cfg(config)
    sym_cfg = _symbol_dynamic_overrides(cfg, symbol)
    factor = float(sym_cfg.get("scale_lot_factor", cfg.get("scale_lot_factor", 0.9)))
    if layer <= 0:
        return base_lot
    return round(base_lot * (factor ** layer), 2)