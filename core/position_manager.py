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
from core.utils import (
    append_archive_record,
    read_json_state,
    utc_now_iso,
    write_json_state,
)

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore


# ----- Tier-2 mgmt archive (2026-07-20) ---------------------------------------
# Append-only JSONL log at state/position_mgmt_archive.jsonl. One record per
# *finalised* mgmt_row — written BEFORE _save_mgmt_state so a downstream reader
# can recover BE/Partial/Stale flags at merge time even after the live
# mgmt_state has rolled. Idempotent via transaction_id. See scripts/
# backfill_mgmt_archive.py for retro-backfill on historical closes.
import uuid as _uuid
_ARCHIVE_FILENAME = "position_mgmt_archive.jsonl"
_ARCHIVE_REASONS = (
    "be_trail_update",
    "partial_tp",
    "tp_close",
    "time_stop",
    "mt5_close_sync",
    "backfill",
)


def _archive_mgmt_row(  # noqa: PLR0913
    ticket: str | int,
    mgmt_row: dict[str, Any],
    *,
    reason: str,
    side: str | None = None,
    entry: float | None = None,
    symbol: str | None = None,
):
    """Persist the current mgmt_row to state/position_mgmt_archive.jsonl.

    Called from all 4 close/exit paths. Failure to write a record is logged
    but NEVER raises — the live bookkeeping MUST keep going.

    The record's ``mgmt_row`` keeps every flag the dashboard Profit Quality
    pane needs (be_triggered, partial_tp_done, trailing, peak_price,
    trail_distance, initial_sl, risk_distance_floor, stale_closed, _audit).
    """
    if reason not in _ARCHIVE_REASONS:
        reason = "be_trail_update"  # safe default
    transaction_id = _uuid.uuid4().hex[:12]  # 48 bits: birthday @ ~16M ids
    entry_price = entry if entry is not None else (
        float(mgmt_row.get("entry")) if isinstance(mgmt_row.get("entry"), (int, float)) else None
    )
    initial_sl = mgmt_row.get("initial_sl")
    risk_distance_floor = None
    if (
        entry_price is not None
        and initial_sl is not None
        and isinstance(initial_sl, (int, float))
    ):
        try:
            risk_distance_floor = round(abs(entry_price - float(initial_sl)), 8)
        except (TypeError, ValueError):
            risk_distance_floor = None
    snapshot = {
        "break_even": bool(mgmt_row.get("break_even")),
        "partial_tp_done": bool(mgmt_row.get("partial_tp_done")),
        "trailing": bool(mgmt_row.get("trailing")),
        "stale_closed": bool(mgmt_row.get("stale_closed")) or (reason == "time_stop"),
        "initial_sl": float(initial_sl) if isinstance(initial_sl, (int, float)) else None,
        "peak_price": mgmt_row.get("peak_price"),
        "worst_price": mgmt_row.get("worst_price"),
        "mfe_R": mgmt_row.get("mfe_R"),
        "mae_R": mgmt_row.get("mae_R"),
        "trail_distance": mgmt_row.get("trail_distance"),
        "risk_distance_floor": risk_distance_floor,
        "partial_closed_volume": mgmt_row.get("partial_closed_volume"),
        "runner_tp": mgmt_row.get("runner_tp"),
        "last_modify_fail_sl": mgmt_row.get("last_modify_fail_sl"),
        "_audit": list(mgmt_row.get("_audit") or []),
    }
    record = {
        "ticket": str(ticket),
        "transaction_id": transaction_id,
        "reason": reason,
        "side": side,
        "entry": entry_price,
        "symbol": symbol,
        "mgmt_row": snapshot,
    }
    try:
        append_archive_record(_ARCHIVE_FILENAME, record)
        return transaction_id
    except (OSError, ValueError, TypeError) as _exc:
        try:
            logging.getLogger("position_manager").warning(
                "Archive write FAILED ticket=%s reason=%s err=%s",
                ticket, reason, _exc,
            )
        except Exception:
            pass
        return None


def _trading_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("trading", {})


def _sltp_cfg(config: dict[str, Any]) -> dict[str, Any]:
    """Per-symbol SL/TP ratios (tp1_rr, tp2_rr, sl_atr_mult)."""
    return _trading_cfg(config).get("strategy_entries", {}).get("sl_tp", {})


def _fallback_tp(
    pos: dict[str, Any],
    config: dict[str, Any],
    tp_level: int,
    field: str,
) -> float:
    """Generic TP1/TP2 fallback. ``tp_level=1`` reads ``tp1_rr``, ``=2`` reads ``tp2_rr``."""
    try:
        cur = float(pos.get(field) or 0)
    except (TypeError, ValueError):
        cur = 0.0
    if cur > 0:
        return cur
    try:
        entry = float(pos.get("entry") or 0)
        sl = float(pos.get("sl") or 0)
    except (TypeError, ValueError):
        return 0.0
    if entry <= 0 or sl <= 0:
        return 0.0
    sl_tp = _sltp_cfg(config)
    if not isinstance(sl_tp, dict):
        return 0.0
    symbol = pos.get("symbol") or ""
    per_sym = sl_tp.get("per_symbol", {}).get(symbol, {}) if isinstance(sl_tp.get("per_symbol"), dict) else {}
    rr_key = f"tp{tp_level}_rr"
    rr = float(per_sym.get(rr_key, sl_tp.get(rr_key, 2.5 if tp_level == 2 else 1.5)))
    side = pos.get("side")
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0
    if side == "BUY":
        return entry + risk * rr
    if side == "SELL":
        return entry - risk * rr
    return 0.0


def _fallback_tp1(pos: dict[str, Any], config: dict[str, Any]) -> float:
    """Compute TP1 from config when position.tp1=0 or missing (review fix: same failure mode as tp2)."""
    return _fallback_tp(pos, config, 1, "tp1")


def _fallback_tp2(pos: dict[str, Any], config: dict[str, Any]) -> float:
    """Compute TP2 from config when position.tp2=0 or missing.

    Some open paths (fast_mode, MT5 mirror, recovery scripts) skip the
    strategy_entry SL/TP block, leaving tp2 unset. Without a TP2 target the
    runner can never lock in a big win — partial TP closes 50% at TP1 then
    leaves the runner with no defined exit. Falling back to config.tp2_rr
    (with per-symbol overrides) gives the runner a 2.5R target by default,
    matching the strategy_entry path so positions behave identically.
    """
    return _fallback_tp(pos, config, 2, "tp2")


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
    if prof.get("break_even_lock_r") is not None:
        be_out["lock_profit_atr_mult"] = float(prof["break_even_lock_r"])
    # Points-based profile knobs (scenario / ghost promotion)
    for k in ("trigger_points", "lock_profit_points", "trigger_atr_mult", "lock_profit_atr_mult"):
        if prof.get(k) is not None:
            be_out[k] = prof[k]
    if prof.get("trail_start_r") is not None:
        trail_out["activation_atr_mult"] = float(prof["trail_start_r"])
    if prof.get("trail_atr_mult") is not None:
        trail_out["trail_points_atr_mult"] = float(prof["trail_atr_mult"])
        trail_out["trail_atr_mult"] = float(prof["trail_atr_mult"])
    for k in ("activation_points", "activation_atr_mult", "trail_points", "trail_points_atr_mult"):
        if prof.get(k) is not None:
            trail_out[k] = prof[k]
    if prof.get("trailing_enabled") is False:
        trail_out["enabled"] = False
    elif prof.get("trailing_enabled") is True:
        trail_out["enabled"] = True
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


def _exit_trigger_met_pct(
    side: str,
    entry: float,
    current: float,
    trigger_pct: float,
) -> bool:
    """Price-percent BE trigger — true when price moved >= trigger_pct
    (e.g. 0.0015 = +0.15%) in the favourable direction.

    Independent of SL width so the trigger fires at the SAME price-percent
    across symbols regardless of how tight or wide the stop is. Used as a
    defensive 'lock at BE' signal so small winners don't reverse to losers
    — the user's "lock the trade at breakeven once it goes +0.15% up" intent.
    """
    if trigger_pct <= 0 or entry <= 0 or current <= 0:
        return False
    if side == "BUY":
        return (current - entry) / entry >= trigger_pct
    if side == "SELL":
        return (entry - current) / entry >= trigger_pct
    return False


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


def _update_worst_price(
    side: str,
    entry: float,
    current_price: float,
    mgmt_row: dict[str, Any],
) -> float:
    """Track worst (most adverse) price reached for MAE calculation.

    Mirrors ``_update_peak_price`` but ratchets in the *adverse* direction:
    for a BUY, worst_price is the lowest price seen (price dipped against us);
    for a SELL, worst_price is the highest price seen.
    """
    worst = mgmt_row.get("worst_price")
    if worst is None:
        worst = entry
    worst = float(worst)
    if side == "BUY":
        return min(worst, current_price)
    return max(worst, current_price)


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
    # Ghost promotions from trade_manager (wider BE/trail that beat live).
    try:
        from core.trade_manager import merge_promotion_into_mgmt

        be_sym, trail_sym, _promo = merge_promotion_into_mgmt(be_sym, trail_sym, symbol)
    except Exception:
        pass

    # Distance-first: when *broker points* are configured, null USD triggers
    # entirely so OR-semantics cannot fire BE/trail early (review bug: $25 USD
    # was firing before 5000-pt gold BE). ATR-only symbols keep USD OR distance.
    tm = (config.get("trade_manager") or {})
    if tm.get("enabled", True) and tm.get("distance_first_triggers", True):
        pts = be_sym.get("trigger_points", be_cfg.get("trigger_points"))
        if pts is not None:
            be_sym = dict(be_sym)
            be_sym["trigger_profit_usd"] = None
            # lock can stay USD-or-points for post-trigger lock size
        act_pts = trail_sym.get("activation_points", trail_cfg.get("activation_points"))
        if act_pts is not None:
            trail_sym = dict(trail_sym)
            trail_sym["activation_profit_usd"] = None

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
    # ----- MAE/MFE tracking (2026-07-21) ----------------------------------
    # Track intra-bar peak/worst price on every tick so ``calibrate_be_trail.py``
    # can compute BE trigger R from realised path excursions. Both are tracked
    # unconditionally — not only when BE/trail are armed — because the history
    # is needed even for trades that never triggered either excursion management.
    peak = _update_peak_price(side, entry, current_price, row)
    row["peak_price"] = peak
    worst = _update_worst_price(side, entry, current_price, row)
    row["worst_price"] = worst
    risk_dist = abs(float(risk_sl) - entry) if float(risk_sl) != entry else 0.0
    if risk_dist > 0.0:
        if side == "BUY":
            row["mfe_R"] = round((peak - entry) / risk_dist, 4)
            row["mae_R"] = round((entry - worst) / risk_dist, 4)
        else:
            row["mfe_R"] = round((entry - peak) / risk_dist, 4)
            row["mae_R"] = round((worst - entry) / risk_dist, 4)

    if be_cfg.get("enabled", True):
        be_pct_trigger_raw = float(be_cfg.get("trigger_pct", 0) or 0)
        be_sym_pct = float(be_sym.get("trigger_pct", be_pct_trigger_raw) or 0)
        be_pct_hit = be_sym_pct > 0 and _exit_trigger_met_pct(
            side, entry, current_price, be_sym_pct,
        )
        be_hit = (
            row.get("break_even", False)
            or _exit_trigger_met(
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
            or be_pct_hit
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
            if be_pct_hit:
                # Percent-triggered BE: lock SL exactly at entry (or at the
                # explicit lock_profit_points buffer if the user set one).
                # The user's intent is "the trade is free once it moves
                # +0.15% in my favour" — so the lock should be BE, not
                # BE + 0.10R. Honour explicit points-based lock overrides.
                if lock_pts is not None:
                    lock = lock_pts
                elif usd_triggered and usd_lock_raw is not None:
                    lock = max(0.0, float(usd_lock_raw))
                else:
                    lock = 0.0
            elif lock_pts is not None:
                lock = lock_pts
            elif usd_triggered and usd_lock_raw is not None:
                lock = max(0.0, float(usd_lock_raw))
            else:
                lock = atr * float(be_sym.get("lock_profit_atr_mult", be_cfg.get("lock_profit_atr_mult", 0.1)))
            # ----- PAYOFF PARADOX PATCH (2026-07-20) ----------------------------
            # Patch (a): NEVER close a winning position at less than
            # `trading.exits.min_r_multiple_win` R-multiples. If the lock BE
            # would set is BELOW the floor AND the position is in profit
            # (`profit_dist > 0`), the BE move is suppressed entirely so the
            # trade rides until higher R.
            # ----- 2026-07-21 EXTENSION (percent trigger) ---------------------
            # When `trigger_pct` fires the BE, the lock is at exactly entry
            # (BREAK EVEN). This is the user's explicit defensive intent —
            # they asked for "lock at BE once +0.15% in profit" so a
            # reversal cannot lose money. The floor gate IS bypassed for
            # this path because the floor's purpose ("don't pre-cap winners
            # below 0.4R") fights the user's "+0.15% makes me safe" intent.
            min_r_win = float(
                ((config.get("trading") or {}).get("exits") or {})
                .get("min_r_multiple_win", 0.4)
            )
            try:
                risk_distance_floor = abs(entry - float(risk_sl))
            except (TypeError, ValueError):
                risk_distance_floor = 0.0
            below_win_floor = (
                min_r_win > 0
                and risk_distance_floor > 0
                and lock < min_r_win * risk_distance_floor
                and profit_dist > 0
                and not be_pct_hit
            )
            if risk_distance_floor <= 0 and profit_dist > 0 and min_r_win > 0 and not be_pct_hit:
                row.setdefault("_audit", []).append({
                    "kind": "payoff_paradox_unpriced",
                    "reason": "missing initial_sl + current_sl; BE-floor gate bypassed",
                })
            if be_pct_hit and not row.get("be_pct_fired"):
                # Dedupe: log the percent-trigger event ONCE so long-running
                # trades don"t grow unbounded audit lists across hundreds
                # of cycles above the threshold.
                row["be_pct_fired"] = True
                row.setdefault("_audit", []).append({
                    "kind": "be_pct_trigger",
                    "lock_r": round(lock, 6),
                    "trigger_pct": be_sym_pct,
                })
            if not below_win_floor:
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
        # Stamp TP2 into mgmt_row from config if missing — persistent across
        # paper_positions.json overwrites (MT5 sync, fast_mode, recovery).
        # Without this, the partial-TP runner has no target and sits forever.
        if not row.get("tp2"):
            tp2 = _fallback_tp2(pos, config)
            if tp2 > 0:
                row["tp2"] = tp2
                pos["tp2"] = tp2

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
        # Tier-2 (REVIEW FIX): archive ONLY on a state-change so the per-cycle
        # poll for idle paper positions doesn't flood the archive at
        # ~14 writes/cycle. Same pattern as manage_mt5_positions status=updat.
        if (
            new_sl is not None
            or actions
            or row.get("break_even")
            or row.get("trailing")
            or row.get("partial_tp_done")
        ):
            _archive_mgmt_row(
                ticket, row,
                reason="be_trail_update",
                side=pos.get("side"),
                entry=float(pos.get("entry", 0) or 0),
                symbol=symbol,
            )
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
        # Stamp TP2 into mgmt_row from config if missing — persistent across
        # MT5 position re-syncs. Without this, the partial-TP runner has no
        # target and the bot can never lock in a big win.
        if not row.get("tp2"):
            tp2 = _fallback_tp2(pos, config)
            if tp2 > 0:
                row["tp2"] = tp2
                pos["tp2"] = tp2
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
        _mp = pos.get("management_profile") or {}
        _mhm = float(_mp.get("max_hold_minutes") or (config.get("risk") or {}).get("stale_close_minutes") or (config.get("risk") or {}).get("max_order_age_minutes") or 30)
        if age > _mhm * 60 and profit_usd < 0:
            # Prefer shared broker close path (correct filling enum, not raw bitmask).
            try:
                from core.mt5_broker import MT5Broker

                _broker = MT5Broker(config, logger)
                _cr = _broker.close_position(
                    ticket,
                    symbol,
                    side,
                    float(pos.get("size") or pos.get("volume") or 0.01),
                    reason="time_stop",
                )
                if _cr.get("success"):
                    logger.info(
                        "TIME STOP close: #%s %s %s age=%.0fmin profit=$%.2f",
                        ticket, symbol, side, age / 60, profit_usd,
                    )
                    summary["audit"].append({
                        "ticket": ticket, "symbol": symbol, "side": side,
                        "profit_usd": round(profit_usd, 2), "age_sec": round(age, 0),
                        "status": "time_stop_closed", "actions": ["time_stop"],
                    })
                    summary["actions"].append({"ticket": ticket, "symbol": symbol, "actions": ["time_stop"]})
                    summary["updated"] += 1
                    # Tier-2: time-stop close is the canonical Stale axis source.
                    _archive_mgmt_row(
                        ticket, mgmt_row.get("positions", {}).get(ticket_key, {}) or {},
                        reason="time_stop",
                        side=side, entry=float(pos.get("entry", 0) or 0), symbol=symbol,
                    )
                    continue
                _err = str(_cr.get("error") or "time_stop_failed")
                logger.warning("TIME STOP close FAILED: #%s %s -> %s", ticket, symbol, _err)
                summary["errors"].append({"ticket": ticket, "error": "time_stop: " + _err})
            except Exception as _exc:
                logger.warning("TIME STOP close FAILED: #%s %s -> %s", ticket, symbol, _exc)
                summary["errors"].append({"ticket": ticket, "error": f"time_stop: {_exc}"})
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
        # Tier-2: every successful MT5 SL move is a near-final mgmt_row.
        # Archive BEFORE _save_mgmt_state so a synchronous close wins.
        _archive_mgmt_row(
            ticket, row,
            reason="be_trail_update",
            side=side, entry=float(pos.get("entry", 0) or 0), symbol=symbol,
        )
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
    """Scale out at TP1; extend runner to TP2 with a locked-profit SL.

    When ``partial_tp.enabled`` is OFF, falls back to a full 100% close at TP1
    so winners don't sit indefinitely. This is the active path under
    ``execution.mode: mt5`` — even though MT5 handles TP natively via the
    limit order set at position open, this code path is still required when
    the open-time TP wasn't attached (or was attached at a stale price), and
    it's the only channel that explicitly emits a partial/full-TP trade record.
    """
    logger = logger or logging.getLogger("position_manager")
    summary: dict[str, Any] = {
        "partial_closes": 0,
        "full_closes": 0,
        "actions": [],
        "errors": [],
        "timestamp": utc_now_iso(),
    }
    if mt5 is None:
        return summary
    if not partial_tp_enabled(config):
        # Partial TP off: same full-close-at-TP1 fallback as the paper path.
        # Without this, winners sit until the broker's open-time TP fires —
        # which is fine in steady state, but on stale/missing TP attachments
        # the bot had no recovery path.
        full_close_only = True
        fraction = 1.0
    else:
        full_close_only = False
        fraction = float(
            config.get("trading", {})
            .get("exits", {})
            .get("partial_tp", {})
            .get("fraction", 0.5)
        )

    from core.exit_manager import runner_cfg

    mgmt = _load_mgmt_state()
    order_index = _load_order_index()
    run = runner_cfg(config)
    pcfg = config.get("trading", {}).get("exits", {}).get("partial_tp", {})

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
        # TP2 fallback: if the position record has tp2=0 (e.g. opened via
        # fast_mode, MT5 mirror, or recovery script that skips the
        # strategy_entry SL/TP block), compute it from config.tp2_rr so the
        # runner gets a target and can lock in a big win.
        if tp2 <= 0:
            tp2 = _fallback_tp2(pos, config)
            if tp2 > 0:
                pos["tp2"] = tp2
        if full_close_only:
            # Bypass partial_close_volume for the full-close case so the
            # ``min_remain`` floor doesn't suppress 100% exits on minimum-lot
            # positions. Round to step/lot size for MT5 compliance.
            close_vol = max(vmin, round(size / vstep) * vstep)
        else:
            close_vol = partial_close_volume(
                size,
                fraction,
                volume_min=vmin,
                volume_step=vstep,
                min_remain=float(pcfg.get("min_volume_remain", vmin)),
            )
            # FALLBACK: when partial_close_volume rounds to 0 (size < 2 * vmin
            # so it can't split), promote to a single 100% close at TP1. Fixes
            # 0.01-lot positions where partial would silently skip.
            if close_vol <= 0 and size >= vmin:
                close_vol = max(vmin, round(size / vstep) * vstep)
                full_close_only = True
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
        if full_close_only:
            summary["full_closes"] += 1
            row["full_close_only"] = True
        else:
            summary["partial_closes"] += 1
        summary["actions"].append({
            "ticket": pos["ticket"],
            "symbol": symbol,
            "closed_volume": close_vol,
            "lock_sl": lock_sl,
            "runner_tp": new_tp,
            "full_close": full_close_only,
        })
        # Tier-2: archive reason reflects what actually happened. partial_tp for
        # scale-out, tp_close for the full-exit fallback. Downstream readers
        # (Profit Quality dashboard, merge-time recovery in trade_tracker) key
        # off this reason to set the right axis flag.
        archive_reason = "tp_close" if full_close_only else "partial_tp"
        _archive_mgmt_row(
            pos["ticket"], row,
            reason=archive_reason,
            side=side, entry=entry, symbol=symbol,
        )
        if full_close_only:
            logger.info(
                "Full close at TP1 (partial_tp off) %s ticket=%s closed=%.2f",
                symbol, pos["ticket"], close_vol,
            )
        else:
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
    """Paper partial TP1 — reduce size, bank partial, arm runner.

    When ``partial_tp.enabled`` is OFF, falls back to a full 100% close on TP1
    so profitable positions still exit (rather than sitting indefinitely because
    the partial path was the only TP-execution channel for paper mode).
    """
    logger = logger or logging.getLogger("position_manager")
    summary: dict[str, Any] = {
        "partial_closes": 0,
        "full_closes": 0,
        "actions": [],
        "timestamp": utc_now_iso(),
    }
    partial_trades: list[dict[str, Any]] = []
    if not partial_tp_enabled(config):
        # Don't return early — close 100% at TP1 (single-shot full exit instead
        # of partial scale-out). This is the only TP-execution channel for paper
        # mode; without this, winners sit until trail/SL moves them.
        full_close_only = True
        fraction = 1.0
    else:
        full_close_only = False
        fraction = float(
            config.get("trading", {})
            .get("exits", {})
            .get("partial_tp", {})
            .get("fraction", 0.5)
        )

    from core.exit_manager import runner_cfg

    mgmt = _load_mgmt_state()
    run = runner_cfg(config)
    pcfg = config.get("trading", {}).get("exits", {}).get("partial_tp", {})
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
            # TP2 fallback: if the paper position record has tp2=0 (opened via
            # fast_mode or another path that skipped the SL/TP block), compute
            # it from config.tp2_rr so the runner has a big-win target.
            if tp2 <= 0:
                tp2 = _fallback_tp2(pos, config)
                if tp2 > 0:
                    pos["tp2"] = tp2
                    new_pos["tp2"] = tp2
            size = float(pos.get("size", 0.01))
            if full_close_only:
                # Bypass partial_close_volume for the full-close case so the
                # ``min_remain`` floor doesn't suppress 100% exits on 0.01-lot
                # positions. Fraction=1.0 here means the user explicitly
                # disabled partial TP, so they want a single full exit.
                close_vol = size
            else:
                close_vol = partial_close_volume(
                    size,
                    fraction,
                    volume_min=0.01,
                    volume_step=0.01,
                    min_remain=float(pcfg.get("min_volume_remain", 0.01)),
                )
                # FALLBACK: when partial rounds to 0 (size < 2 * vmin, can't
                # split), promote to a single 100% close at TP1. Fixes 0.01-lot
                # positions like XAUUSDm / BTCUSDm / NAS100m where partial
                # would silently skip.
                if close_vol <= 0 and size >= 0.01:
                    close_vol = size
                    full_close_only = True
            if close_vol > 0:
                atr = float(feat.get("atr", price * 0.001) or price * 0.001)
                risk_sl = float(row.get("initial_sl") or current_sl or entry)
                lock_sl = post_partial_sl(side, entry, risk_sl, atr, config)
                diff = (tp1 - entry) if side == "BUY" else (entry - tp1)
                partial_pnl = diff * close_vol
                if full_close_only:
                    # Single-shot full exit. exit_reason/partial flag reflect
                    # what actually happened (a full TP1 close, not a partial
                    # scale-out). Same archive reason as the MT5 path so the
                    # dashboard's TP-axis bucket lights up consistently.
                    exit_reason = "take_profit"
                    is_partial = False
                else:
                    exit_reason = "partial_take_profit"
                    is_partial = True
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
                    "exit_reason": exit_reason,
                    "setup_type": pos.get("setup_type"),
                    "partial": is_partial,
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
                if full_close_only:
                    row["full_close_only"] = True
                if row.get("initial_sl") is None and current_sl > 0:
                    row["initial_sl"] = current_sl
                if full_close_only:
                    summary["full_closes"] += 1
                else:
                    summary["partial_closes"] += 1
                summary["actions"].append({
                    "ticket": ticket,
                    "symbol": symbol,
                    "closed_volume": close_vol,
                    "full_close": full_close_only,
                })
                # Tier-2: archive reason reflects what actually happened.
                archive_reason = "tp_close" if full_close_only else "partial_tp"
                _archive_mgmt_row(
                    ticket, row,
                    reason=archive_reason,
                    side=side, entry=entry, symbol=symbol,
                )
                if full_close_only:
                    logger.info(
                        "Paper FULL close at TP1 (partial_tp off) %s ticket=%s pnl=%.2f",
                        symbol, ticket, partial_pnl,
                    )
                else:
                    logger.info("Paper partial TP1 %s ticket=%s closed=%.2f", symbol, ticket, close_vol)

        mgmt.setdefault("positions", {})[ticket] = row
        updated.append(new_pos)

    mgmt["timestamp"] = utc_now_iso()
    mgmt["last_partial_run"] = summary
    _save_mgmt_state(mgmt)
    return updated, partial_trades, summary