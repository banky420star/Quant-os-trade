"""Adaptive Exit Engine — per-symbol ATR-based TP/SL with spread floor, broker stop level, and 70%→BE trigger.

Replaces the old fixed-RR approach with symbol-specific ATR multipliers from the
user's calibrated table.  Every symbol gets its own SL distance, M5 TP, M15 TP,
and minimum-TP-as-spread-multiple so the bot adapts to each instrument's volatility
profile instead of using one-size-fits-all risk-reward ratios.

Calculation (matching the user's MQL5 spec)::

    ATRPoints = ATRPrice / Point

    TPPoints = MathMax(
        ATRPoints * TP_ATR_Multiplier,
        SpreadPoints * MinimumSpreadMultiplier
    )

    SLPoints = MathMax(
        ATRPoints * SL_ATR_Multiplier,
        SpreadPoints * 4.0
    )

    # Also respect broker's minimum stop distance
    TPPoints = MathMax(TPPoints, stop_level + 1)
    SLPoints = MathMax(SLPoints, stop_level + 1)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("adaptive_exit")

# ---------------------------------------------------------------------------
# Per-symbol default table  (user's calibrated values)
# ---------------------------------------------------------------------------
# Keys:
#   tp_m5_atr       — M5 TP as ATR multiple
#   tp_m15_atr      — M15 TP as ATR multiple (when bias = M15)
#   sl_atr          — SL distance as ATR multiple
#   spread_min_mult — minimum TP as multiple of spread points
#   trail_atr       — ATR trailing distance after BE (default 0.25)
#   be_trigger_pct  — % of TP at which to move SL to BE (default 0.70)
#   max_spread_tp_ratio — reject trade if spread / TP > this (default 0.25)
#
_ADAPTIVE_DEFAULTS: dict[str, dict[str, float]] = {
    # R:R = tp_m5_atr / sl_atr.  All symbols set to >= 1.5:1 so every trade has
    # positive mathematical expectancy regardless of entry quality.
    "XAUUSDm":  {"tp_m5_atr": 1.20, "tp_m15_atr": 1.80, "sl_atr": 0.80, "spread_min_mult": 3.5, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
    "USOILm":   {"tp_m5_atr": 1.28, "tp_m15_atr": 1.92, "sl_atr": 0.85, "spread_min_mult": 3.5, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
    "BTCUSDm":  {"tp_m5_atr": 1.50, "tp_m15_atr": 2.25, "sl_atr": 1.00, "spread_min_mult": 4.0, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
    "EURUSDm":  {"tp_m5_atr": 1.05, "tp_m15_atr": 1.58, "sl_atr": 0.70, "spread_min_mult": 3.0, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
    "GBPUSDm":  {"tp_m5_atr": 1.13, "tp_m15_atr": 1.69, "sl_atr": 0.75, "spread_min_mult": 3.0, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
    "USDJPYm":  {"tp_m5_atr": 0.60, "tp_m15_atr": 0.90, "sl_atr": 0.40, "spread_min_mult": 3.0, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
    "USDCHFm":  {"tp_m5_atr": 0.60, "tp_m15_atr": 0.90, "sl_atr": 0.40, "spread_min_mult": 3.0, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
    "AUDUSDm":  {"tp_m5_atr": 1.05, "tp_m15_atr": 1.58, "sl_atr": 0.70, "spread_min_mult": 3.0, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
    "US500m":   {"tp_m5_atr": 1.80, "tp_m15_atr": 2.70, "sl_atr": 1.20, "spread_min_mult": 3.5, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
    "US30m":    {"tp_m5_atr": 1.80, "tp_m15_atr": 2.70, "sl_atr": 1.20, "spread_min_mult": 4.0, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
    "NAS100m":  {"tp_m5_atr": 1.50, "tp_m15_atr": 2.25, "sl_atr": 1.00, "spread_min_mult": 4.0, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
    "UK100m":   {"tp_m5_atr": 1.28, "tp_m15_atr": 1.92, "sl_atr": 0.85, "spread_min_mult": 3.5, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
    "FR40m":    {"tp_m5_atr": 1.28, "tp_m15_atr": 1.92, "sl_atr": 0.85, "spread_min_mult": 3.5, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
    "JP225m":   {"tp_m5_atr": 1.50, "tp_m15_atr": 2.25, "sl_atr": 1.00, "spread_min_mult": 4.0, "trail_atr": 0.25, "be_trigger_pct": 0.70, "max_spread_tp_ratio": 0.25},
}

# Broker point defaults (mirrors position_manager._DEFAULT_BROKER_POINTS)
_BROKER_POINTS: dict[str, float] = {
    "EURUSDm": 1e-5, "GBPUSDm": 1e-5, "USDCHFm": 1e-5, "AUDUSDm": 1e-5,
    "USDJPYm": 0.001, "XAUUSDm": 0.01, "USOILm": 0.01, "BTCUSDm": 0.01,
    "US500m": 0.1, "US30m": 1.0, "NAS100m": 0.01, "UK100m": 0.1, "FR40m": 0.1,
}


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------
def _trading_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("trading") or {}


def _adaptive_cfg(config: dict[str, Any]) -> dict[str, Any]:
    block = (_trading_cfg(config).get("adaptive_exit") or {})
    return block if isinstance(block, dict) else {}


def adaptive_exit_enabled(config: dict[str, Any]) -> bool:
    """Master switch — when False, falls back to the old strategy_entry SL/TP."""
    return bool(_adaptive_cfg(config).get("enabled", True))


def get_symbol_config(symbol: str, config: dict[str, Any]) -> dict[str, float]:
    """Resolve adaptive exit settings for a single symbol.

    Precedence: config.per_symbol > hardcoded defaults > global fallback.
    """
    base = dict(_ADAPTIVE_DEFAULTS.get(symbol, {}))
    if not base:
        # Global fallback for unknown symbols
        base = {"tp_m5_atr": 0.35, "tp_m15_atr": 0.50, "sl_atr": 0.80,
                "spread_min_mult": 3.5, "trail_atr": 0.25, "be_trigger_pct": 0.70,
                "max_spread_tp_ratio": 0.25}
    ac = _adaptive_cfg(config)
    global_overrides = {k: v for k, v in ac.items() if k not in ("enabled", "per_symbol") and k in base}
    base.update(global_overrides)
    per = (ac.get("per_symbol") or {}).get(symbol) or {}
    if isinstance(per, dict):
        for k in base:
            if k in per:
                base[k] = float(per[k])
    return base


def broker_point(symbol: str) -> float | None:
    """Return broker point size for a symbol (or None if unknown)."""
    return _BROKER_POINTS.get(symbol)


# ---------------------------------------------------------------------------
# Core calculations
# ---------------------------------------------------------------------------
def compute_adaptive_levels(
    symbol: str,
    side: str,
    entry: float,
    atr: float,
    price: float,
    *,
    point: float | None = None,
    spread_points: int = 0,
    stops_level: int = 0,
    freeze_level: int = 0,
    bias_timeframe: str = "M5",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute SL and TP for a trade using per-symbol adaptive ATR multipliers.

    Returns::

        {
            "sl": float,          # stop loss price
            "tp1": float,         # take-profit target price
            "tp2": float,         # secondary target (tp1 * 1.5 from same risk)
            "sl_points": int,     # SL distance in broker points
            "tp_points": int,     # TP distance in broker points
            "expected_loss_usd": float,   # per-unit SL cost estimate
            "expected_profit_usd": float, # per-unit TP1 profit estimate
            "spread_pct_of_tp": float,    # spread / TP ratio (0-1)
            "symbol_cfg": dict,   # resolved config for debugging
        }
    """
    cfg = get_symbol_config(symbol, config or {})
    _point = point if point is not None else broker_point(symbol)
    if not _point or _point <= 0:
        _point = 0.0001  # fallback

    # ATR → broker points
    atr_points = int(round(atr / _point)) if atr > 0 and _point > 0 else 0

    # Select TP multiplier based on bias timeframe
    tp_atr_mult = cfg.get("tp_m15_atr", 0.45) if bias_timeframe.upper() == "M15" else cfg.get("tp_m5_atr", 0.30)
    sl_atr_mult = cfg.get("sl_atr", 0.80)
    spread_min_mult = cfg.get("spread_min_mult", 3.5)
    trail_atr = cfg.get("trail_atr", 0.25)

    # TP and SL in broker points
    tp_atr_points = int(round(atr_points * tp_atr_mult)) if atr_points > 0 else 0
    sl_atr_points = int(round(atr_points * sl_atr_mult)) if atr_points > 0 else 0

    # Minimum TP = spread × multiplier
    min_tp_points = int(round(spread_points * spread_min_mult)) if spread_points > 0 else 0

    # Apply floor: TP = max(ATR-based, spread-floor, broker stop level)
    tp_points = max(tp_atr_points, min_tp_points)
    if stops_level > 0:
        tp_points = max(tp_points, stops_level + 1)

    # SL = max(ATR-based, spread × 4, broker stop level)
    min_sl_points = int(round(spread_points * 4.0)) if spread_points > 0 else 0
    sl_points = max(sl_atr_points, min_sl_points)
    if stops_level > 0:
        sl_points = max(sl_points, stops_level + 1)

    # Convert back to price
    if side == "BUY":
        sl = entry - sl_points * _point
        tp1 = entry + tp_points * _point
    else:
        sl = entry + sl_points * _point
        tp1 = entry - tp_points * _point

    # TP2 = same risk distance × (tp_atr_mult × 2.0) — doubles the target
    tp2_atr_mult = tp_atr_mult * 2.0
    tp2_atr_points = int(round(atr_points * tp2_atr_mult)) if atr_points > 0 else 0
    tp2_points = max(tp2_atr_points, min_tp_points)
    if stops_level > 0:
        tp2_points = max(tp2_points, stops_level + 1)
    if side == "BUY":
        tp2 = entry + tp2_points * _point
    else:
        tp2 = entry - tp2_points * _point

    # Spread / TP ratio
    spread_pct_of_tp = (spread_points / max(tp_points, 1)) if tp_points > 0 else 0.0

    # Estimated USD values (rough: price × points × lot)
    # Per 0.01 lot standard, $1 ≈ 10000 points on forex, varies per symbol
    risk_points = abs(entry - sl) / _point if _point > 0 else 0
    reward_points = abs(tp1 - entry) / _point if _point > 0 else 0

    return {
        "sl": round(sl, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "sl_points": int(round(risk_points)),
        "tp_points": int(round(reward_points)),
        "spread_pct_of_tp": round(spread_pct_of_tp, 4),
        "trail_atr": trail_atr,
    }


# ---------------------------------------------------------------------------
# 70% TP → Breakeven trigger
# ---------------------------------------------------------------------------
def should_trigger_be_at_70pct_tp(
    side: str,
    entry: float,
    tp1: float,
    current_price: float,
) -> bool:
    """True when price has reached >= 70% of TP distance.

    This triggers the SL move to breakeven so the trade is free.
    """
    if tp1 == entry:
        return False
    tp_distance = abs(tp1 - entry)
    if tp_distance <= 0:
        return False
    if side == "BUY":
        progress = (current_price - entry) / tp_distance
    else:
        progress = (entry - current_price) / tp_distance
    return progress >= 0.70


def compute_be_at_70pct_sl(
    side: str,
    entry: float,
) -> float:
    """Return the SL price that locks breakeven.

    For BUY: SL = entry (no loss).  For SELL: SL = entry.
    """
    return entry
