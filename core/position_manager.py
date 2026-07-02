"""Break-even and per-symbol trailing stop management."""

from __future__ import annotations

import logging
from typing import Any

from core.blue_guardian import blue_guardian_enabled, can_modify_position_sl
from core.symbol_manager import broker_symbol
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


# Live data-driven overrides for BE/trailing (calibrate_be_trail.py ->
# state/symbol_be_trail_live.json). Honored only when ``trusted`` (n>=50 +
# beats seed + ci95 lo>0). Same pattern as strategy_entry._sltp_cfg.
_LIVE_CACHE: dict[str, Any] = {"path": None, "data": {}}


def _load_live_mgmt() -> dict[str, Any]:
    """Read symbol_be_trail_live.json once per process (cached by mtime via
    read_json_state's own freshness; we cache the parsed object to avoid a disk
    read on every open position every loop tick)."""
    data = read_json_state("symbol_be_trail_live.json", default={}) or {}
    return data if isinstance(data, dict) else {}


def _merge_live(be_sym: dict[str, Any], trail_sym: dict[str, Any],
                symbol: str, live: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Merge a trusted live BE/trailing override into the per-symbol dicts.

    Returns (be_sym, trail_sym, applied). Only keys present in the override and
    marked trusted are merged; the seed values are otherwise left intact.
    """
    if not isinstance(live, dict):
        return be_sym, trail_sym, False
    sym_live = (live.get("symbols") or {}).get(symbol) if isinstance(live.get("symbols"), dict) else None
    if not isinstance(sym_live, dict) or not sym_live.get("trusted"):
        return be_sym, trail_sym, False
    applied = False
    be_live = sym_live.get("break_even") if isinstance(sym_live.get("break_even"), dict) else None
    tr_live = sym_live.get("trailing") if isinstance(sym_live.get("trailing"), dict) else None
    if be_live:
        for k in (
            "trigger_profit_usd",
            "lock_profit_usd",
            "trigger_points",
            "lock_profit_points",
            "trigger_atr_mult",
            "lock_profit_atr_mult",
        ):
            if k in be_live:
                be_sym[k] = be_live[k]
                applied = True
    if tr_live:
        for k in (
            "activation_profit_usd",
            "activation_points",
            "activation_atr_mult",
            "trail_atr_mult",
            "trail_points",
            "trail_points_atr_mult",
            "trail_use_atr",
        ):
            if k in tr_live:
                trail_sym[k] = tr_live[k]
                applied = True
    return be_sym, trail_sym, applied


def _normalize_price(value: float, digits: int) -> float:
    return round(value, digits)


# Blue Guardian defaults (used for paper mode when MT5 symbol_info is unavailable).
_DEFAULT_BROKER_POINTS: dict[str, float] = {
    "EURUSDm": 1e-5,
    "GBPUSDm": 1e-5,
    "USDCHFm": 1e-5,
    "AUDUSDm": 1e-5,
    "USDJPYm": 0.001,
    "XAUUSDm": 0.01,
    "USOILm": 0.01,
    "BTCUSDm": 0.01,
    "US500m": 0.1,
    "US30m": 1.0,
    "NAS100m": 0.01,
    "UK100m": 0.1,
    "FR40m": 0.1,
}


def _default_broker_point(symbol: str) -> float | None:
    return _DEFAULT_BROKER_POINTS.get(symbol)


def _points_to_price(points: float | int | None, point: float | None) -> float | None:
    if points is None or not point or point <= 0:
        return None
    return float(points) * point


def _profit_trigger_met(
    profit_usd: float | None,
    profit_dist: float,
    sym_cfg: dict[str, Any],
    parent_cfg: dict[str, Any],
    usd_key: str,
    points_key: str,
) -> bool | None:
    """USD when profit is known, else MT5 points; None => caller uses ATR mult."""
    usd_raw = sym_cfg.get(usd_key, parent_cfg.get(usd_key))
    if profit_usd is not None and usd_raw is not None:
        return profit_usd >= float(usd_raw)
    return None


def _distance_trigger_met(
    profit_dist: float,
    sym_cfg: dict[str, Any],
    parent_cfg: dict[str, Any],
    points_key: str,
    atr_mult_key: str,
    atr: float,
    point: float | None,
) -> bool:
    pts_raw = sym_cfg.get(points_key, parent_cfg.get(points_key))
    pts_price = _points_to_price(pts_raw, point)
    if pts_price is not None:
        return profit_dist >= pts_price
    mult = float(sym_cfg.get(atr_mult_key, parent_cfg.get(atr_mult_key, 0.5)))
    return profit_dist >= atr * mult


def _trail_distance_price(
    trail_sym: dict[str, Any],
    trail_cfg: dict[str, Any],
    atr: float,
    point: float | None = None,
) -> float:
    """Trail offset in price units.

    Priority: ``trail_points_atr_mult`` (dynamic) -> ``trail_points`` (MT5 pts)
    -> legacy ``trail_use_atr`` / ``trail_atr_mult``.
    """
    atr_mult = trail_sym.get("trail_points_atr_mult", trail_cfg.get("trail_points_atr_mult"))
    if atr_mult is not None:
        return float(atr_mult) * atr

    if trail_sym.get("trail_use_atr") or trail_cfg.get("trail_use_atr"):
        mult = float(trail_sym.get("trail_atr_mult", trail_cfg.get("trail_atr_mult", 0.35)))
        return atr * mult

    raw_pts = trail_sym.get("trail_points", trail_cfg.get("trail_points"))
    if raw_pts is not None:
        if not point or point <= 0:
            raise ValueError("trail_points requires a positive broker point size")
        return float(raw_pts) * point

    mult = float(trail_sym.get("trail_atr_mult", trail_cfg.get("trail_atr_mult", 0.35)))
    if point and point > 0 and mult < 1.0:
        return mult * 100.0 * point
    return atr * mult


def _update_peak_price(
    side: str,
    entry: float,
    current_price: float,
    mgmt_row: dict[str, Any],
) -> float:
    """Ratchet peak favourable price for trailing (matches paper_broker)."""
    peak = mgmt_row.get("peak_price")
    if peak is None:
        peak = entry
    peak = float(peak)
    if side == "BUY":
        return max(peak, current_price)
    return min(peak, current_price)


def _profit_distance(side: str, entry: float, current: float) -> float:
    if side == "BUY":
        return current - entry
    return entry - current


def _floating_profit_usd(position: dict[str, Any]) -> float | None:
    raw = position.get("profit")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


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
    point: float | None = None,
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
    # Data-driven live override (scripts/calibrate_be_trail.py). Trusted only.
    be_sym, trail_sym, _applied = _merge_live(be_sym, trail_sym, symbol, _load_live_mgmt())

    if point is None:
        point = _default_broker_point(symbol)

    profit_dist = _profit_distance(side, entry, current_price)
    profit_usd = _floating_profit_usd(position)
    actions: list[str] = []
    row = dict(mgmt_row)
    new_sl = current_sl

    if be_cfg.get("enabled", True):
        be_usd = _profit_trigger_met(
            profit_usd, profit_dist, be_sym, be_cfg, "trigger_profit_usd", "trigger_points",
        )
        be_hit = be_usd is True or (
            be_usd is None
            and _distance_trigger_met(
                profit_dist, be_sym, be_cfg, "trigger_points", "trigger_atr_mult", atr, point,
            )
        )
        if be_hit:
            lock_pts = _points_to_price(be_sym.get("lock_profit_points", be_cfg.get("lock_profit_points")), point)
            if lock_pts is not None:
                lock = lock_pts
            elif be_usd is True:
                lock = 0.0
            else:
                lock = atr * float(be_sym.get("lock_profit_atr_mult", be_cfg.get("lock_profit_atr_mult", 0.1)))
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
        trail_usd = _profit_trigger_met(
            profit_usd, profit_dist, trail_sym, trail_cfg, "activation_profit_usd", "activation_points",
        )
        trail_dist = _trail_distance_price(trail_sym, trail_cfg, atr, point)
        trail_hit = trail_usd is True or (
            trail_usd is None
            and _distance_trigger_met(
                profit_dist, trail_sym, trail_cfg, "activation_points", "activation_atr_mult", atr, point,
            )
        )
        if trail_hit:
            row["trailing"] = True
            peak = _update_peak_price(side, entry, current_price, row)
            row["peak_price"] = peak
            if side == "BUY":
                trail_sl = peak - trail_dist
                if trail_sl > new_sl:
                    new_sl = trail_sl
            else:
                trail_sl = peak + trail_dist
                if current_sl <= 0 or trail_sl < new_sl:
                    new_sl = trail_sl
            if row.get("trailing"):
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

        point = _default_broker_point(symbol)
        new_sl, row, actions = compute_managed_sl(config, pos, price, atr, row, point=point)
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
        broker_sym = broker_symbol(symbol)
        ticket = int(pos["ticket"])
        feat = features.get("symbols", {}).get(symbol, {})
        if not mt5.symbol_select(broker_sym, True):
            continue
        tick = mt5.symbol_info_tick(broker_sym)
        info = mt5.symbol_info(broker_sym)
        if tick is None or info is None:
            continue

        side = pos["side"]
        current = float(tick.bid if side == "SELL" else tick.ask)
        atr = float(feat.get("atr", current * 0.001) or current * 0.001)
        digits = int(getattr(info, "digits", 5))
        row = dict(mgmt.get("positions", {}).get(str(ticket), {}))

        if blue_guardian_enabled(config) and not can_modify_position_sl(config, pos):
            continue

        point = float(getattr(info, "point", 0) or 0)
        new_sl, row, actions = compute_managed_sl(config, pos, current, atr, row, point=point)
        if new_sl is None or not actions:
            continue

        new_sl = _normalize_price(new_sl, digits)
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": broker_sym,
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