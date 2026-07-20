"""Break-even and per-symbol trailing stop management."""

from __future__ import annotations

import logging
from typing import Any

from core.blue_guardian import (
    blue_guardian_enabled,
    blue_guardian_settings,
    can_modify_position_sl,
    position_age_seconds,
)
from core.exit_manager import (
    partial_close_volume,
    partial_tp_enabled,
    post_partial_sl,
    profit_rr,
    resolve_tp_levels,
    tp1_reached,
    trail_activation_allowed,
    trail_distance_multiplier,
)
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


def _merge_management_profile(
    be_sym: dict[str, Any],
    trail_sym: dict[str, Any],
    position: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply per-trade management_profile from evaluation_loop when present."""
    meta = position.get("signal_meta") or {}
    prof = position.get("management_profile") or meta.get("management_profile")
    if not isinstance(prof, dict) or not prof:
        return be_sym, trail_sym
    be_out = dict(be_sym)
    trail_out = dict(trail_sym)
    if prof.get("break_even_trigger_r") is not None:
        be_out["trigger_atr_mult"] = float(prof["break_even_trigger_r"])
    if prof.get("trail_start_r") is not None:
        trail_out["activation_atr_mult"] = float(prof["trail_start_r"])
    if prof.get("trail_atr_mult") is not None:
        trail_out["trail_points_atr_mult"] = float(prof["trail_atr_mult"])
    return be_out, trail_out


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


def _min_stop_distance_price(
    point: float,
    *,
    stops_level: int = 0,
    spread_points: int = 0,
    freeze_level: int = 0,
    min_points_fallback: int = 20,
) -> float:
    """Broker minimum SL distance when ``trade_stops_level`` is zero (common on Exness)."""
    if point <= 0:
        return 0.0
    parts = [min_points_fallback * point]
    if stops_level > 0:
        parts.append(stops_level * point)
    if freeze_level > 0:
        parts.append(freeze_level * point)
    if spread_points > 0:
        parts.append(spread_points * point)
    return max(parts)


def _clamp_sl_to_stops_level(
    side: str,
    sl: float,
    *,
    reference: float,
    point: float,
    stops_level: int,
    digits: int,
    spread_points: int = 0,
    freeze_level: int = 0,
) -> float:
    """Enforce MT5 minimum SL distance from current bid/ask.

    BUY SL must be at or below ``reference - min_dist`` (use bid).
    SELL SL must be at or above ``reference + min_dist`` (use ask).

    Some brokers (e.g. Exness USOILm) report ``trade_stops_level=0``; we still
    enforce spread / fallback distance so trail SL cannot sit below market.
    """
    sl = _normalize_price(sl, digits)
    min_dist = _min_stop_distance_price(
        point,
        stops_level=stops_level,
        spread_points=spread_points,
        freeze_level=freeze_level,
    )
    if min_dist <= 0:
        return sl
    if side == "BUY":
        cap = reference - min_dist
        if sl > cap:
            sl = cap
    else:
        floor = reference + min_dist
        if sl < floor:
            sl = floor
    return _normalize_price(sl, digits)


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


def _exit_trigger_met(
    profit_usd: float | None,
    profit_dist: float,
    sym_cfg: dict[str, Any],
    parent_cfg: dict[str, Any],
    *,
    usd_key: str,
    points_key: str,
    atr_mult_key: str,
    atr: float,
    point: float | None,
) -> bool:
    """True when USD profit OR broker-point distance OR ATR-mult distance is met."""
    usd_raw = sym_cfg.get(usd_key, parent_cfg.get(usd_key))
    if profit_usd is not None and usd_raw is not None and profit_usd >= float(usd_raw):
        return True
    return _distance_trigger_met(
        profit_dist, sym_cfg, parent_cfg, points_key, atr_mult_key, atr, point,
    )


def _trail_distance_price(
    trail_sym: dict[str, Any],
    trail_cfg: dict[str, Any],
    atr: float,
    point: float | None = None,
) -> float:
    """Trail offset in price units behind peak.

    Priority:
      1. ``trail_points_atr_mult`` — dynamic MT5 broker points = round(ATR/point × mult)
      2. ``trail_points`` — fixed MT5 broker points × ``symbol_info.point``
      3. ``trail_use_atr`` / ``trail_atr_mult`` — legacy ATR fraction (research only)
    """
    atr_mult = trail_sym.get("trail_points_atr_mult", trail_cfg.get("trail_points_atr_mult"))
    if atr_mult is not None:
        if point and point > 0 and atr > 0:
            broker_pts = max(1, int(round((atr / point) * float(atr_mult))))
            return broker_pts * point
        return float(atr_mult) * atr

    raw_pts = trail_sym.get("trail_points", trail_cfg.get("trail_points"))
    if raw_pts is not None:
        if not point or point <= 0:
            raise ValueError("trail_points requires a positive broker point size")
        return float(raw_pts) * point

    if trail_sym.get("trail_use_atr") or trail_cfg.get("trail_use_atr"):
        mult = float(trail_sym.get("trail_atr_mult", trail_cfg.get("trail_atr_mult", 0.35)))
        return atr * mult

    mult = float(trail_sym.get("trail_atr_mult", trail_cfg.get("trail_atr_mult", 0.35)))
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


def _load_open_times() -> dict[str, str]:
    data = read_json_state("position_open_times.json", default={}) or {}
    return data if isinstance(data, dict) else {}


def _enrich_position_open_time(
    position: dict[str, Any],
    open_times: dict[str, str],
) -> dict[str, Any]:
    """Use agent-recorded UTC open time so min_hold / trail gates are correct."""
    ticket = str(position.get("ticket") or position.get("position_id") or "")
    opened = open_times.get(ticket)
    if opened:
        return {**position, "opened_at": opened}
    return position


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
    be_sym, trail_sym = _merge_management_profile(be_sym, trail_sym, position)
    # Data-driven live override (scripts/calibrate_be_trail.py). Trusted only.
    be_sym, trail_sym, _applied = _merge_live(be_sym, trail_sym, symbol, _load_live_mgmt())

    if point is None:
        point = _default_broker_point(symbol)

    profit_dist = _profit_distance(side, entry, current_price)
    profit_usd = _floating_profit_usd(position)
    actions: list[str] = []
    row = dict(mgmt_row)
    if row.get("initial_sl") is None and current_sl > 0:
        row["initial_sl"] = current_sl
    risk_sl = float(row.get("initial_sl") or current_sl or entry)
    new_sl = current_sl

    if be_cfg.get("enabled", True):
        be_hit = row.get("break_even", False) or _exit_trigger_met(
            profit_usd,
            profit_dist,
            be_sym,
            be_cfg,
            usd_key="trigger_profit_usd",
            points_key="trigger_points",
            atr_mult_key="trigger_atr_mult",
            atr=atr,
            point=point,
        )
        if be_hit:
            lock_pts = _points_to_price(be_sym.get("lock_profit_points", be_cfg.get("lock_profit_points")), point)
            usd_lock_raw = be_sym.get("lock_profit_usd", be_cfg.get("lock_profit_usd"))
            usd_trig_raw = be_sym.get("trigger_profit_usd", be_cfg.get("trigger_profit_usd"))
            usd_triggered = (
                profit_usd is not None
                and usd_trig_raw is not None
                and profit_usd >= float(usd_trig_raw)
            )
            if lock_pts is not None:
                lock = lock_pts
            elif usd_triggered and usd_lock_raw is not None:
                lock = max(0.0, float(usd_lock_raw))
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
        trail_dist = _trail_distance_price(trail_sym, trail_cfg, atr, point)
        trail_dist *= trail_distance_multiplier(row, config)
        rr = profit_rr(side, entry, risk_sl, current_price)
        trail_armed = trail_activation_allowed(
            row,
            profit_rr_value=rr,
            profit_usd=profit_usd,
            profit_dist=profit_dist,
            trail_sym=trail_sym,
            trail_cfg=trail_cfg,
            atr=atr,
            point=point,
            config=config,
            exit_trigger_met=_exit_trigger_met,
        )
        if trail_armed:
            row["trailing"] = True
            peak = _update_peak_price(side, entry, current_price, row)
            row["peak_price"] = peak
            row["trail_distance"] = round(trail_dist, 8)
            if side == "BUY":
                trail_sl = peak - trail_dist
                if trail_sl > new_sl:
                    new_sl = trail_sl
            else:
                trail_sl = peak + trail_dist
                if current_sl <= 0 or trail_sl < new_sl:
                    new_sl = trail_sl
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
    open_times = _load_open_times()
    summary: dict[str, Any] = {
        "updated": 0,
        "actions": [],
        "errors": [],
        "audit": [],
        "timestamp": utc_now_iso(),
    }

    for pos in positions:
        pos = _enrich_position_open_time(pos, open_times)
        symbol = pos["symbol"]
        broker_sym = broker_symbol(symbol)
        ticket = int(pos["ticket"])
        ticket_key = str(ticket)
        feat = features.get("symbols", {}).get(symbol, {})
        if not mt5.symbol_select(broker_sym, True):
            continue
        tick = mt5.symbol_info_tick(broker_sym)
        info = mt5.symbol_info(broker_sym)
        if tick is None or info is None:
            continue

        side = pos["side"]
        bid = float(tick.bid)
        ask = float(tick.ask)
        # Favourable price for peak/trail ratchet (bid for SELL, ask for BUY).
        current = bid if side == "SELL" else ask
        atr = float(feat.get("atr", current * 0.001) or current * 0.001)
        digits = int(getattr(info, "digits", 5))
        row = dict(mgmt.get("positions", {}).get(ticket_key, {}))
        current_sl = float(pos.get("sl", 0))
        profit_usd = float(pos.get("profit", 0) or 0)

        point = float(getattr(info, "point", 0) or 0)
        stops_level = int(getattr(info, "trade_stops_level", 0) or 0)
        freeze_level = int(getattr(info, "trade_freeze_level", 0) or 0)
        spread_points = int(getattr(info, "spread", 0) or 0)
        new_sl, row, actions = compute_managed_sl(config, pos, current, atr, row, point=point)
        mgmt.setdefault("positions", {})[ticket_key] = row

        age = position_age_seconds(pos, open_times)
        min_hold = (
            blue_guardian_settings(config)["min_hold_seconds"]
            if blue_guardian_enabled(config)
            else 0
        )
        can_modify = not blue_guardian_enabled(config) or can_modify_position_sl(config, pos)

        audit_entry: dict[str, Any] = {
            "ticket": ticket,
            "symbol": symbol,
            "side": side,
            "profit_usd": round(profit_usd, 2),
            "age_sec": round(age, 0),
            "trailing": bool(row.get("trailing")),
            "break_even": bool(row.get("break_even")),
            "peak_price": row.get("peak_price"),
            "trail_distance": row.get("trail_distance"),
            "current_sl": current_sl,
            "computed_sl": new_sl,
            "actions": actions,
        }

        if not can_modify:
            audit_entry["status"] = "min_hold_pending"
            audit_entry["min_hold_sec"] = min_hold
            summary["audit"].append(audit_entry)
            if row.get("trailing") or row.get("break_even"):
                logger.info(
                    "Trail armed pending min_hold %s %s ticket=%s age=%.0fs need=%ds profit=$%.2f",
                    symbol, side, ticket, age, min_hold, profit_usd,
                )
            continue

        if new_sl is None or not actions:
            audit_entry["status"] = "monitoring" if row.get("trailing") else "waiting_activation"
            summary["audit"].append(audit_entry)
            if row.get("trailing"):
                logger.debug(
                    "Trail active no SL move %s ticket=%s sl=%s peak=%s",
                    symbol, ticket, current_sl, row.get("peak_price"),
                )
            continue

        # MT5 rejects SL inside trade_stops_level / below ask (SELL) — clamp to market.
        sl_ref = bid if side == "BUY" else ask
        clamped_sl = _clamp_sl_to_stops_level(
            side,
            new_sl,
            reference=sl_ref,
            point=point,
            stops_level=stops_level,
            digits=digits,
            spread_points=spread_points,
            freeze_level=freeze_level,
        )
        min_dist = _min_stop_distance_price(
            point,
            stops_level=stops_level,
            spread_points=spread_points,
            freeze_level=freeze_level,
        )
        new_sl = clamped_sl
        last_fail = row.get("last_modify_fail_sl")
        if last_fail is not None and abs(float(last_fail) - new_sl) < min_dist * 0.25:
            audit_entry["status"] = "modify_backoff"
            summary["audit"].append(audit_entry)
            continue

        # Re-check ratchet still improved SL after broker clamp.
        if side == "BUY" and new_sl <= current_sl:
            audit_entry["status"] = "clamped_no_improvement"
            summary["audit"].append(audit_entry)
            continue
        if side == "SELL" and current_sl > 0 and new_sl >= current_sl:
            audit_entry["status"] = "clamped_no_improvement"
            summary["audit"].append(audit_entry)
            continue

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
            audit_entry["status"] = "modify_failed"
            audit_entry["error"] = err
            summary["audit"].append(audit_entry)
            row["last_modify_fail_sl"] = new_sl
            mgmt.setdefault("positions", {})[ticket_key] = row
            logger.warning(
                "MT5 SL modify failed ticket=%s: %s (sl=%s ref=%s stops=%s spread_pts=%s)",
                ticket, err, new_sl, sl_ref, stops_level, spread_points,
            )
            continue

        row.pop("last_modify_fail_sl", None)
        mgmt.setdefault("positions", {})[ticket_key] = row
        summary["updated"] += 1
        action_rec = {
            "ticket": ticket,
            "symbol": symbol,
            "actions": actions,
            "sl": new_sl,
        }
        summary["actions"].append(action_rec)
        audit_entry["status"] = "updated"
        audit_entry["new_sl"] = new_sl
        summary["audit"].append(audit_entry)
        logger.info(
            "MT5 manage %s %s ticket=%s sl=%s (%s) profit=$%.2f trail=%s",
            symbol, side, ticket, new_sl, ",".join(actions), profit_usd,
            row.get("trailing", False),
        )

    mgmt["timestamp"] = utc_now_iso()
    mgmt["last_run"] = summary
    _save_mgmt_state(mgmt)
    trailing_active = sum(1 for a in summary["audit"] if a.get("trailing"))
    logger.info(
        "Position manager done: %d SL updates, %d trailing armed, %d errors",
        summary["updated"], trailing_active, len(summary["errors"]),
    )
    return summary


def _load_order_index() -> dict[str, dict[str, Any]]:
    orders_state = read_json_state("paper_orders.json", default={}) or {}
    index: dict[str, dict[str, Any]] = {}
    for order in orders_state.get("orders") or []:
        tkt = order.get("mt5_ticket")
        if tkt and order.get("status") == "filled":
            index[str(tkt)] = order
    return index


def manage_partial_tp_mt5(
    config: dict[str, Any],
    positions: list[dict[str, Any]],
    features: dict[str, Any],
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Scale out at TP1; extend runner to TP2 with a locked-profit SL."""
    logger = logger or logging.getLogger("position_manager")
    summary: dict[str, Any] = {
        "partial_closes": 0,
        "actions": [],
        "errors": [],
        "timestamp": utc_now_iso(),
    }
    if not partial_tp_enabled(config) or mt5 is None:
        return summary

    from core.exit_manager import runner_cfg

    mgmt = _load_mgmt_state()
    order_index = _load_order_index()
    run = runner_cfg(config)
    pcfg = config.get("trading", {}).get("exits", {}).get("partial_tp", {})
    fraction = float(pcfg.get("fraction", 0.5))

    for pos in positions:
        ticket_key = str(pos["ticket"])
        row = dict(mgmt.get("positions", {}).get(ticket_key, {}))
        if row.get("partial_tp_done"):
            continue

        symbol = pos["symbol"]
        broker_sym = broker_symbol(symbol)
        if not mt5.symbol_select(broker_sym, True):
            continue
        tick = mt5.symbol_info_tick(broker_sym)
        info = mt5.symbol_info(broker_sym)
        if tick is None or info is None:
            continue

        side = pos["side"]
        entry = float(pos["entry"])
        current_sl = float(pos.get("sl", 0))
        bid = float(tick.bid)
        ask = float(tick.ask)
        current = ask if side == "BUY" else bid
        order = order_index.get(ticket_key)
        tp1, tp2 = resolve_tp_levels(pos, order)
        if not tp1_reached(side, current, tp1):
            continue

        size = float(pos.get("size") or pos.get("volume") or 0)
        vmin = float(info.volume_min or 0.01)
        vstep = float(info.volume_step or 0.01)
        close_vol = partial_close_volume(
            size,
            fraction,
            volume_min=vmin,
            volume_step=vstep,
            min_remain=float(pcfg.get("min_volume_remain", vmin)),
        )
        if close_vol <= 0:
            continue

        if side == "BUY":
            order_type = mt5.ORDER_TYPE_SELL
            price = bid
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = ask
        close_req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": broker_sym,
            "volume": close_vol,
            "type": order_type,
            "position": int(pos["ticket"]),
            "price": price,
            "deviation": int(config.get("execution", {}).get("deviation", 20)),
            "magic": int(config.get("execution", {}).get("magic_number", 20250625)),
            "comment": "qagent_partial_tp1",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        close_result = mt5.order_send(close_req)
        if close_result is None or close_result.retcode != mt5.TRADE_RETCODE_DONE:
            err = str(mt5.last_error()) if close_result is None else f"{close_result.retcode} {close_result.comment}"
            summary["errors"].append({"ticket": pos["ticket"], "error": err})
            logger.warning("Partial TP close failed ticket=%s: %s", pos["ticket"], err)
            continue

        feat = features.get("symbols", {}).get(symbol, {})
        atr = float(feat.get("atr", current * 0.001) or current * 0.001)
        risk_sl = float(row.get("initial_sl") or current_sl or entry)
        lock_sl = post_partial_sl(side, entry, risk_sl, atr, config)
        digits = int(getattr(info, "digits", 5))
        point = float(getattr(info, "point", 0) or 0)
        stops_level = int(getattr(info, "trade_stops_level", 0) or 0)
        freeze_level = int(getattr(info, "trade_freeze_level", 0) or 0)
        spread_points = int(getattr(info, "spread", 0) or 0)
        lock_sl = _clamp_sl_to_stops_level(
            side,
            lock_sl,
            reference=bid if side == "BUY" else ask,
            point=point,
            stops_level=stops_level,
            digits=digits,
            spread_points=spread_points,
            freeze_level=freeze_level,
        )
        new_tp = float(tp2) if run.get("extend_tp_to_tp2", True) and tp2 > 0 else float(pos.get("tp1", tp1))

        mod_req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": int(pos["ticket"]),
            "symbol": broker_sym,
            "sl": lock_sl,
            "tp": new_tp,
        }
        mod_result = mt5.order_send(mod_req)
        if mod_result is None or mod_result.retcode != mt5.TRADE_RETCODE_DONE:
            err = str(mt5.last_error()) if mod_result is None else f"{mod_result.retcode} {mod_result.comment}"
            summary["errors"].append({"ticket": pos["ticket"], "error": f"post_partial_sltp:{err}"})
            logger.warning("Post-partial SLTP failed ticket=%s: %s", pos["ticket"], err)

        row["partial_tp_done"] = True
        row["partial_closed_volume"] = close_vol
        row["runner_tp"] = new_tp
        row["break_even"] = True
        if row.get("initial_sl") is None and current_sl > 0:
            row["initial_sl"] = current_sl
        mgmt.setdefault("positions", {})[ticket_key] = row
        summary["partial_closes"] += 1
        summary["actions"].append({
            "ticket": pos["ticket"],
            "symbol": symbol,
            "closed_volume": close_vol,
            "lock_sl": lock_sl,
            "runner_tp": new_tp,
        })
        logger.info(
            "Partial TP1 %s ticket=%s closed=%.2f lock_sl=%.5f runner_tp=%.5f",
            symbol, pos["ticket"], close_vol, lock_sl, new_tp,
        )

    mgmt["timestamp"] = utc_now_iso()
    mgmt["last_partial_run"] = summary
    _save_mgmt_state(mgmt)
    return summary


def manage_partial_tp_paper(
    config: dict[str, Any],
    positions: list[dict[str, Any]],
    features: dict[str, Any],
    logger: logging.Logger | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Paper partial TP1 — reduce size, bank partial, arm runner."""
    logger = logger or logging.getLogger("position_manager")
    summary: dict[str, Any] = {
        "partial_closes": 0,
        "actions": [],
        "timestamp": utc_now_iso(),
    }
    partial_trades: list[dict[str, Any]] = []
    if not partial_tp_enabled(config):
        return positions, partial_trades, summary

    from core.exit_manager import runner_cfg

    mgmt = _load_mgmt_state()
    run = runner_cfg(config)
    pcfg = config.get("trading", {}).get("exits", {}).get("partial_tp", {})
    fraction = float(pcfg.get("fraction", 0.5))
    updated: list[dict[str, Any]] = []

    for pos in positions:
        ticket = str(pos.get("position_id") or pos.get("ticket"))
        row = dict(mgmt.get("positions", {}).get(ticket, {}))
        new_pos = dict(pos)
        symbol = pos["symbol"]
        feat = features.get("symbols", {}).get(symbol, {})
        price = float(feat.get("price", pos.get("entry", 0)))
        side = pos["side"]
        entry = float(pos["entry"])
        current_sl = float(pos.get("sl", 0))
        tp1, tp2 = resolve_tp_levels(pos)

        if not row.get("partial_tp_done") and tp1_reached(side, price, tp1):
            size = float(pos.get("size", 0.01))
            close_vol = partial_close_volume(
                size,
                fraction,
                volume_min=0.01,
                volume_step=0.01,
                min_remain=float(pcfg.get("min_volume_remain", 0.01)),
            )
            if close_vol > 0:
                atr = float(feat.get("atr", price * 0.001) or price * 0.001)
                risk_sl = float(row.get("initial_sl") or current_sl or entry)
                lock_sl = post_partial_sl(side, entry, risk_sl, atr, config)
                diff = (tp1 - entry) if side == "BUY" else (entry - tp1)
                partial_pnl = diff * close_vol
                partial_trades.append({
                    "trade_id": f"partial-{ticket}-{utc_now_iso()}",
                    "position_id": ticket,
                    "symbol": symbol,
                    "side": side,
                    "entry": entry,
                    "exit": tp1,
                    "sl": current_sl,
                    "tp1": tp1,
                    "pnl": round(partial_pnl, 2),
                    "result": "win" if partial_pnl > 0 else "loss",
                    "exit_reason": "partial_take_profit",
                    "setup_type": pos.get("setup_type"),
                    "partial": True,
                    "closed_at": utc_now_iso(),
                })
                new_pos["size"] = round(size - close_vol, 2)
                new_pos["sl"] = _normalize_price(lock_sl, 5)
                new_pos["tp1"] = tp2 if run.get("extend_tp_to_tp2", True) and tp2 > 0 else tp1
                new_pos["tp2"] = tp2
                new_pos["be_triggered"] = True
                row["partial_tp_done"] = True
                row["partial_closed_volume"] = close_vol
                row["break_even"] = True
                if row.get("initial_sl") is None and current_sl > 0:
                    row["initial_sl"] = current_sl
                summary["partial_closes"] += 1
                summary["actions"].append({"ticket": ticket, "symbol": symbol, "closed_volume": close_vol})
                logger.info("Paper partial TP1 %s ticket=%s closed=%.2f", symbol, ticket, close_vol)

        mgmt.setdefault("positions", {})[ticket] = row
        updated.append(new_pos)

    mgmt["timestamp"] = utc_now_iso()
    mgmt["last_partial_run"] = summary
    _save_mgmt_state(mgmt)
    return updated, partial_trades, summary