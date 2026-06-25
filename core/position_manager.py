"""Break-even and per-symbol trailing stop management."""

from __future__ import annotations

import logging
from typing import Any

from core.utils import read_json_state, utc_now_iso, write_json_state

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore


def _trading_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("trading", {})


def _be_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return _trading_cfg(config).get("break_even", {})


def _trail_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return _trading_cfg(config).get("trailing", {})


def _symbol_overrides(cfg: dict[str, Any], symbol: str) -> dict[str, Any]:
    return dict(cfg.get("per_symbol", {}).get(symbol, {}))


def _normalize_price(value: float, digits: int) -> float:
    return round(value, digits)


def _profit_distance(side: str, entry: float, current: float) -> float:
    if side == "BUY":
        return current - entry
    return entry - current


def _load_mgmt_state() -> dict[str, Any]:
    return read_json_state("position_management.json", default={"positions": {}})


def _save_mgmt_state(state: dict[str, Any]) -> None:
    write_json_state("position_management.json", state)


def compute_managed_sl(
    config: dict[str, Any],
    position: dict[str, Any],
    current_price: float,
    atr: float,
    mgmt_row: dict[str, Any],
) -> tuple[float | None, dict[str, Any], list[str]]:
    """
    Compute new SL for break-even + trailing.

    Returns (new_sl or None, updated_mgmt_row, actions).
    """
    symbol = position["symbol"]
    side = position["side"]
    entry = float(position["entry"])
    current_sl = float(position.get("sl", 0))
    be_cfg = _be_cfg(config)
    trail_cfg = _trail_cfg(config)
    be_sym = _symbol_overrides(be_cfg, symbol)
    trail_sym = _symbol_overrides(trail_cfg, symbol)

    profit_dist = _profit_distance(side, entry, current_price)
    actions: list[str] = []
    row = dict(mgmt_row)
    new_sl = current_sl

    if be_cfg.get("enabled", True):
        trigger = atr * float(be_sym.get("trigger_atr_mult", be_cfg.get("trigger_atr_mult", 0.5)))
        lock = atr * float(be_sym.get("lock_profit_atr_mult", be_cfg.get("lock_profit_atr_mult", 0.1)))
        if profit_dist >= trigger:
            if side == "BUY":
                be_sl = entry + lock
                if be_sl > new_sl:
                    new_sl = be_sl
                    row["break_even"] = True
                    actions.append("break_even")
            else:
                be_sl = entry - lock
                if current_sl <= 0 or be_sl < new_sl:
                    new_sl = be_sl
                    row["break_even"] = True
                    actions.append("break_even")

    if trail_cfg.get("enabled", True):
        activation = atr * float(trail_sym.get("activation_atr_mult", trail_cfg.get("activation_atr_mult", 0.75)))
        trail_dist = atr * float(trail_sym.get("trail_atr_mult", trail_cfg.get("trail_atr_mult", 0.35)))
        if profit_dist >= activation:
            if side == "BUY":
                trail_sl = current_price - trail_dist
                if trail_sl > new_sl:
                    new_sl = trail_sl
                    row["trailing"] = True
                    actions.append("trail")
            else:
                trail_sl = current_price + trail_dist
                if current_sl <= 0 or trail_sl < new_sl:
                    new_sl = trail_sl
                    row["trailing"] = True
                    actions.append("trail")

    if abs(new_sl - current_sl) < 1e-9:
        return None, row, actions

    if side == "BUY" and new_sl <= current_sl:
        return None, row, actions
    if side == "SELL" and current_sl > 0 and new_sl >= current_sl:
        return None, row, actions

    return new_sl, row, actions


def manage_paper_positions(
    config: dict[str, Any],
    positions: list[dict[str, Any]],
    features: dict[str, Any],
    logger: logging.Logger | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply BE/trailing to paper positions in memory."""
    logger = logger or logging.getLogger("position_manager")
    mgmt = _load_mgmt_state()
    updated_positions: list[dict[str, Any]] = []
    summary = {"updated": 0, "actions": [], "timestamp": utc_now_iso()}

    for pos in positions:
        symbol = pos["symbol"]
        ticket = str(pos.get("position_id") or pos.get("ticket"))
        feat = features.get("symbols", {}).get(symbol, {})
        price = float(feat.get("price", pos.get("entry", 0)))
        atr = float(feat.get("atr", price * 0.001) or price * 0.001)
        row = dict(mgmt.get("positions", {}).get(ticket, {}))

        new_sl, row, actions = compute_managed_sl(config, pos, price, atr, row)
        new_pos = dict(pos)
        if new_sl is not None:
            new_pos["sl"] = _normalize_price(new_sl, 5)
            summary["updated"] += 1
            summary["actions"].append({"ticket": ticket, "symbol": symbol, "actions": actions, "sl": new_pos["sl"]})
            logger.info(
                "Paper manage %s %s ticket=%s sl=%.5f (%s)",
                symbol, pos.get("side"), ticket, new_pos["sl"], ",".join(actions),
            )
        mgmt.setdefault("positions", {})[ticket] = row
        updated_positions.append(new_pos)

    mgmt["timestamp"] = utc_now_iso()
    mgmt["last_run"] = summary
    _save_mgmt_state(mgmt)
    return updated_positions, summary


def manage_mt5_positions(
    config: dict[str, Any],
    positions: list[dict[str, Any]],
    features: dict[str, Any],
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Modify MT5 SL for agent positions (break-even + trail)."""
    logger = logger or logging.getLogger("position_manager")
    if mt5 is None:
        raise RuntimeError("MetaTrader5 package not installed")

    mgmt = _load_mgmt_state()
    summary = {"updated": 0, "actions": [], "errors": [], "timestamp": utc_now_iso()}

    for pos in positions:
        symbol = pos["symbol"]
        ticket = int(pos["ticket"])
        feat = features.get("symbols", {}).get(symbol, {})
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None:
            continue

        side = pos["side"]
        current = float(tick.bid if side == "SELL" else tick.ask)
        atr = float(feat.get("atr", current * 0.001) or current * 0.001)
        digits = int(getattr(info, "digits", 5))
        row = dict(mgmt.get("positions", {}).get(str(ticket), {}))

        new_sl, row, actions = compute_managed_sl(config, pos, current, atr, row)
        if new_sl is None or not actions:
            continue

        new_sl = _normalize_price(new_sl, digits)
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": symbol,
            "sl": new_sl,
            "tp": float(pos.get("tp1", pos.get("tp", 0))),
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = str(mt5.last_error()) if result is None else f"{result.retcode} {result.comment}"
            summary["errors"].append({"ticket": ticket, "error": err})
            logger.warning("MT5 SL modify failed ticket=%s: %s", ticket, err)
            continue

        summary["updated"] += 1
        summary["actions"].append({
            "ticket": ticket,
            "symbol": symbol,
            "actions": actions,
            "sl": new_sl,
        })
        mgmt.setdefault("positions", {})[str(ticket)] = row
        logger.info(
            "MT5 manage %s %s ticket=%s sl=%s (%s)",
            symbol, side, ticket, new_sl, ",".join(actions),
        )

    mgmt["timestamp"] = utc_now_iso()
    mgmt["last_run"] = summary
    _save_mgmt_state(mgmt)
    return summary