"""Profit-taking and runner exit logic — partial TP, deferred trail, post-partial locks."""

from __future__ import annotations

import math
from typing import Any


def _trading_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("trading", {})


def exits_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return _trading_cfg(config).get("exits", {})


def partial_tp_cfg(config: dict[str, Any]) -> dict[str, Any]:
    block = exits_cfg(config).get("partial_tp", {})
    return block if isinstance(block, dict) else {}


def runner_cfg(config: dict[str, Any]) -> dict[str, Any]:
    block = exits_cfg(config).get("runner", {})
    return block if isinstance(block, dict) else {}


def defer_trail_cfg(config: dict[str, Any]) -> dict[str, Any]:
    block = exits_cfg(config).get("defer_trail_until", {})
    return block if isinstance(block, dict) else {}


def partial_tp_enabled(config: dict[str, Any]) -> bool:
    return bool(partial_tp_cfg(config).get("enabled", False))


def risk_distance(entry: float, sl: float, side: str) -> float:
    if side == "BUY":
        return max(entry - sl, 0.0)
    return max(sl - entry, 0.0)


def profit_distance(side: str, entry: float, current: float) -> float:
    if side == "BUY":
        return current - entry
    return entry - current


def profit_rr(side: str, entry: float, sl: float, current: float) -> float:
    risk = risk_distance(entry, sl, side)
    if risk <= 0:
        return 0.0
    return profit_distance(side, entry, current) / risk


def tp1_reached(side: str, current: float, tp1: float) -> bool:
    if tp1 <= 0:
        return False
    if side == "BUY":
        return current >= tp1
    return current <= tp1


def tp2_reached(side: str, current: float, tp2: float) -> bool:
    if tp2 <= 0:
        return False
    if side == "BUY":
        return current >= tp2
    return current <= tp2


def partial_close_volume(
    size: float,
    fraction: float,
    *,
    volume_min: float = 0.01,
    volume_step: float = 0.01,
    min_remain: float | None = None,
) -> float:
    """Lots to close at TP1 while leaving a valid runner size."""
    if size <= 0 or fraction <= 0:
        return 0.0
    fraction = min(1.0, float(fraction))
    min_remain = float(min_remain if min_remain is not None else volume_min)
    raw = size * fraction
    steps = math.floor(raw / volume_step + 1e-9)
    close_vol = round(steps * volume_step, 2)
    if close_vol < volume_min:
        return 0.0
    remain = round(size - close_vol, 2)
    if remain < min_remain:
        return 0.0
    return close_vol


def post_partial_lock_distance(
    entry: float,
    sl: float,
    side: str,
    atr: float,
    config: dict[str, Any],
) -> float:
    """Price distance above/below entry to lock on the runner after partial TP."""
    run = runner_cfg(config)
    risk = risk_distance(entry, sl, side)
    lock_rr = float(run.get("lock_profit_rr", 0.35))
    lock_atr = float(run.get("lock_profit_atr_mult", 0.15))
    if risk > 0 and lock_rr > 0:
        return risk * lock_rr
    return atr * lock_atr


def post_partial_sl(
    side: str,
    entry: float,
    sl: float,
    atr: float,
    config: dict[str, Any],
) -> float:
    lock = post_partial_lock_distance(entry, sl, side, atr, config)
    if side == "BUY":
        return entry + lock
    return entry - lock


def trail_distance_multiplier(mgmt_row: dict[str, Any], config: dict[str, Any]) -> float:
    if not mgmt_row.get("partial_tp_done"):
        return 1.0
    return float(runner_cfg(config).get("trail_tighten_mult", 0.65))


def trail_activation_allowed(
    mgmt_row: dict[str, Any],
    *,
    profit_rr_value: float,
    profit_usd: float | None,
    profit_dist: float,
    trail_sym: dict[str, Any],
    trail_cfg: dict[str, Any],
    atr: float,
    point: float | None,
    config: dict[str, Any],
    exit_trigger_met,
) -> bool:
    """Gate trailing so it does not arm at $3 and block TP1."""
    if mgmt_row.get("trailing"):
        return True

    defer = defer_trail_cfg(config)
    if not defer.get("require_partial_or_rr", True):
        return exit_trigger_met(
            profit_usd,
            profit_dist,
            trail_sym,
            trail_cfg,
            usd_key="activation_profit_usd",
            points_key="activation_points",
            atr_mult_key="activation_atr_mult",
            atr=atr,
            point=point,
        )

    min_rr = float(defer.get("min_rr", 0.75))
    if mgmt_row.get("partial_tp_done"):
        return True
    if profit_rr_value >= min_rr:
        return True

    if defer.get("allow_early_usd") and profit_usd is not None:
        early = defer.get("early_activation_profit_usd", trail_cfg.get("activation_profit_usd"))
        if early is not None and profit_usd >= float(early):
            return True

    return False


def resolve_tp_levels(
    position: dict[str, Any],
    order: dict[str, Any] | None = None,
) -> tuple[float, float]:
    """Return (tp1, tp2) from position and/or opening order metadata."""
    order = order or {}
    tp1 = float(position.get("tp1") or position.get("tp") or order.get("tp1") or 0)
    tp2 = float(position.get("tp2") or order.get("tp2") or 0)
    return tp1, tp2


def near_take_profit(
    exit_price: float,
    tp: float,
    *,
    side: str | None = None,
    point: float | None = None,
    tolerance_pct: float = 0.0015,
    max_tolerance_points: int = 80,
) -> bool:
    """True only when exit is on the profitable side of TP within a tight band.

    ``tolerance_pct`` is a fraction of TP price (0.0015 = 0.15%), not 15%.
    """
    if tp <= 0 or exit_price <= 0:
        return False
    tol = abs(tp) * tolerance_pct
    if point and point > 0:
        tol = min(tol, max_tolerance_points * point)
        tol = max(tol, point * 2)
    else:
        tol = max(tol, 1e-6)
    if side == "BUY":
        return exit_price >= tp - tol
    if side == "SELL":
        return exit_price <= tp + tol
    return abs(exit_price - tp) <= tol