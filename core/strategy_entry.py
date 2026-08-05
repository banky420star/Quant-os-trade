"""Strategy-pinned entry prices — entries at technical levels, not blind market price."""

from __future__ import annotations

import logging
from typing import Any

from core.adaptive_exit import (
    adaptive_exit_enabled,
    compute_adaptive_levels,
    get_symbol_config,
)
from core.utils import read_json_state


def strategy_entries_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("trading", {}).get("strategy_entries", {}).get("enabled", True))


def _cfg(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("trading", {}).get("strategy_entries", {})


def _round_price(value: float, digits: int = 5) -> float:
    return round(value, digits)


# Default SL/TP calibration knobs (global). Overridden per-symbol in config
# trading.strategy_entries.sl_tp.per_symbol, and at runtime by a data-driven
# live override in state/symbol_sltp_live.json (written by
# scripts/calibrate_sltp.py once a symbol has enough clean trades).
_SLTP_DEFAULTS = {
    "sl_atr_mult": 0.5,
    "risk_floor_atr_mult": 1.5,
    "risk_floor_pct": 0.001,
    "tp1_rr": 1.5,
    "tp2_rr": 2.5,
}


def _sltp_cfg(config: dict[str, Any], symbol: str | None) -> dict[str, Any]:
    """Resolve SL/TP calibration for a symbol.

    Precedence: data-driven live override (symbol_sltp_live.json, only when
    ``trusted``) > config per_symbol > config global > hardcoded defaults.
    """
    se = _cfg(config)
    block = se.get("sl_tp") if isinstance(se, dict) else None
    base = dict(_SLTP_DEFAULTS)
    if isinstance(block, dict):
        for k in _SLTP_DEFAULTS:
            if k in block:
                base[k] = block[k]
        per = block.get("per_symbol") if isinstance(block.get("per_symbol"), dict) else {}
        if symbol and symbol in per and isinstance(per[symbol], dict):
            base.update({k: per[symbol][k] for k in _SLTP_DEFAULTS if k in per[symbol]})

    # Data-driven live override (calibrate_sltp.py). Only honored when marked
    # trusted (n >= min_n AND beats the seeded default's expectancy).
    try:
        live = read_json_state("symbol_sltp_live.json", default={}) or {}
        if isinstance(live, dict):
            sym_live = (live.get("symbols") or {}).get(symbol) if isinstance(live.get("symbols"), dict) else None
            if isinstance(sym_live, dict) and sym_live.get("trusted"):
                base.update({k: sym_live[k] for k in _SLTP_DEFAULTS if k in sym_live})
    except Exception:
        pass
    return base


def _anchor_for_setup(
    setup_type: str,
    side: str,
    feat: dict[str, Any],
    ctx: dict[str, Any],
) -> tuple[float, str, str]:
    """Return (anchor_price, anchor_name, anchor_reason)."""
    price = float(feat.get("price", 0))
    atr = float(feat.get("atr", price * 0.001) or price * 0.001)
    support = float(feat.get("support", price - atr))
    resistance = float(feat.get("resistance", price + atr))
    bb_mid = float(feat.get("bb_middle", price))
    bb_lower = float(feat.get("bb_lower", support))
    bb_upper = float(feat.get("bb_upper", resistance))
    breakout = feat.get("breakout", "none")

    if setup_type == "pullback":
        if side == "BUY":
            anchor = max(support, bb_mid) if bb_mid <= price else support
            anchor = min(anchor, price)
            return anchor, "support_retest", "Pullback buy — pin entry at structure support / EMA zone"
        anchor = min(resistance, bb_mid) if bb_mid >= price else resistance
        anchor = max(anchor, price)
        return anchor, "resistance_retest", "Pullback sell — pin entry at structure resistance / EMA zone"

    if setup_type == "trend_continuation":
        if side == "BUY":
            anchor = bb_mid if bb_mid < price else price - 0.25 * atr
            return min(anchor, price), "trend_continuation_zone", "Trend continuation buy — entry on value zone (BB mid / shallow dip)"
        anchor = bb_mid if bb_mid > price else price + 0.25 * atr
        return max(anchor, price), "trend_continuation_zone", "Trend continuation sell — entry on value zone (BB mid / shallow pop)"

    if setup_type in ("breakout", "compression_breakout"):
        if side == "BUY":
            level = resistance if breakout in ("breakout", "breakout_retest") else resistance
            return level, "breakout_level", "Breakout buy — entry at broken resistance retest"
        level = support if breakout in ("breakdown", "breakdown_retest") else support
        return level, "breakdown_level", "Breakdown sell — entry at broken support retest"

    if setup_type == "mean_reversion":
        if side == "BUY":
            return bb_lower, "bb_lower", "Mean reversion buy — entry at lower band / stretch"
        return bb_upper, "bb_upper", "Mean reversion sell — entry at upper band / stretch"

    if setup_type == "range_fade":
        if side == "BUY":
            return support, "range_support", "Range fade buy — entry at range support"
        return resistance, "range_resistance", "Range fade sell — entry at range resistance"

    if setup_type == "liquidity_sweep":
        if side == "BUY":
            return support, "liquidity_low", "Liquidity sweep buy — entry after sweep below support"
        return resistance, "liquidity_high", "Liquidity sweep sell — entry after sweep above resistance"

    if setup_type == "false_breakout":
        if side == "BUY":
            return support, "false_breakdown", "False breakdown buy — entry at rejected lows"
        return resistance, "false_breakout", "False breakout sell — entry at rejected highs"

    return price, "market_price", "Default — use current price"


def _levels_from_entry(
    entry: float,
    side: str,
    feat: dict[str, Any],
    atr: float,
    symbol: str | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    """
    Compute SL/TP levels for a trade.

    When adaptive exit is enabled, returns ATR-based levels with spread info.
    Otherwise returns legacy RR-based levels.

    Returns dict with keys: sl, tp1, tp2, spread_pct_of_tp (0 if N/A).
    """
    # ---- ADAPTIVE EXIT ENGINE (2026-07-22) ------------------------------
    if adaptive_exit_enabled(config) and symbol:
        try:
            price = float(feat.get("price", entry))
            spread_points = int(feat.get("spread_points", 0) or 0)
            _adaptive = compute_adaptive_levels(
                symbol=symbol,
                side=side,
                entry=entry,
                atr=atr,
                price=price,
                spread_points=spread_points,
                config=config,
            )
            return {
                "sl": _adaptive["sl"],
                "tp1": _adaptive["tp1"],
                "tp2": _adaptive["tp2"],
                "spread_pct_of_tp": _adaptive["spread_pct_of_tp"],
            }
        except Exception as _aexc:
            logging.getLogger("strategy_entry").warning(
                "Adaptive exit fallback for %s %s: %s", symbol, side, _aexc,
            )

    # ---- LEGACY RR-BASED SL/TP ------------------------------------------
    c = _sltp_cfg(config, symbol)
    sl_atr_mult = float(c.get("sl_atr_mult", 0.5))
    risk_floor_atr_mult = float(c.get("risk_floor_atr_mult", 1.5))
    risk_floor_pct = float(c.get("risk_floor_pct", 0.001))
    tp1_rr = float(c.get("tp1_rr", 1.5))
    tp2_rr = float(c.get("tp2_rr", 2.5))

    support = float(feat.get("support", entry - atr))
    resistance = float(feat.get("resistance", entry + atr))
    risk_floor = max(atr * risk_floor_atr_mult, entry * risk_floor_pct)

    if side == "BUY":
        sl = min(support - sl_atr_mult * atr, entry - risk_floor)
        risk = max(entry - sl, risk_floor)
        tp1 = entry + risk * tp1_rr
        tp2 = entry + risk * tp2_rr
    else:
        sl = max(resistance + sl_atr_mult * atr, entry + risk_floor)
        risk = max(sl - entry, risk_floor)
        tp1 = entry - risk * tp1_rr
        tp2 = entry - risk * tp2_rr

    return {"sl": sl, "tp1": tp1, "tp2": tp2, "spread_pct_of_tp": 0.0}


def resolve_entry_mode(
    entry: float,
    market_price: float,
    atr: float,
    config: dict[str, Any],
) -> str:
    cfg = _cfg(config)
    # A market-only experiment must be authoritative even when a downstream
    # evaluation recipe suggests a limit entry.
    if bool(config.get("execution", {}).get("strategy_entries_market_only", False)):
        return "market"
    if bool(config.get("trading", {}).get("strategy_entries_market_only", False)):
        return "market"
    if cfg.get("use_limit_orders") is False:
        return "market"
    within = float(cfg.get("market_if_within_atr", 0.15)) * atr
    if abs(market_price - entry) <= within:
        return "market"
    return "limit"


def pin_strategy_entry(
    setup_type: str,
    side: str,
    feat: dict[str, Any],
    ctx: dict[str, Any],
    config: dict[str, Any],
    symbol: str | None = None,
) -> dict[str, Any]:
    """
    Compute strategy-pinned entry, SL, TP and whether to use a limit/stop order.

    Entry is anchored to the technical level the setup describes — not the live tick.
    SL/TP calibration (ATR multiples, RR) is resolved per-symbol via
    ``_sltp_cfg`` (config per_symbol + live data-driven override).
    """
    price = float(feat.get("price", 0))
    atr = float(feat.get("atr", price * 0.001) or price * 0.001)
    cfg = _cfg(config)

    anchor, anchor_name, anchor_reason = _anchor_for_setup(setup_type, side, feat, ctx)
    buffer = float(cfg.get("entry_buffer_atr", 0.05)) * atr

    if side == "BUY":
        entry = anchor + buffer if anchor <= price else anchor
    else:
        entry = anchor - buffer if anchor >= price else anchor

    entry = _round_price(entry)
    entry_mode = resolve_entry_mode(entry, price, atr, config)
    if entry_mode == "market":
        entry = _round_price(price)

    # SL/TP must use the final entry (market snap can move entry away from the anchor).
    _levels = _levels_from_entry(entry, side, feat, atr, symbol, config)
    sl = _round_price(_levels["sl"])
    tp1 = _round_price(_levels["tp1"])
    tp2 = _round_price(_levels["tp2"])
    spread_pct_of_tp = _levels.get("spread_pct_of_tp", 0.0)

    max_wait = float(cfg.get("max_entry_wait_atr", 2.0)) * atr
    distance_atr = abs(price - entry) / atr if atr > 0 else 0.0
    within_reach = distance_atr <= float(cfg.get("max_entry_wait_atr", 2.0))

    return {
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "spread_pct_of_tp": spread_pct_of_tp,
        "entry_mode": entry_mode,
        "entry_anchor": anchor_name,
        "entry_anchor_price": _round_price(anchor),
        "entry_reason": anchor_reason,
        "market_price": _round_price(price),
        "distance_atr": round(distance_atr, 3),
        "within_reach": within_reach,
        "order_type": "limit" if entry_mode == "limit" else "market",
    }


def resolve_mt5_pending_type(side: str, entry: float, bid: float, ask: float) -> tuple[int, str]:
    """Map strategy entry vs market to MT5 pending order type name."""
    if side == "BUY":
        if entry <= ask:
            return 2, "buy_limit"  # ORDER_TYPE_BUY_LIMIT
        return 4, "buy_stop"  # ORDER_TYPE_BUY_STOP
    if entry >= bid:
        return 3, "sell_limit"  # ORDER_TYPE_SELL_LIMIT
    return 5, "sell_stop"  # ORDER_TYPE_SELL_STOP