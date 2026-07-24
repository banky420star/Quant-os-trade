"""Entry refinement pass — runs after decision engine, before verifier."""

from __future__ import annotations

import logging
from typing import Any

from core.adaptive_exit import adaptive_exit_enabled
from core.dynamic_entry import refresh_entry_levels, symbol_capacity_available
from core.entry_staging import prune_stale
from core.strategy_entry import pin_strategy_entry, strategy_entries_enabled


def _strategy_entries_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return (config.get("trading") or {}).get("strategy_entries") or {}


def _entry_quality(signal: dict[str, Any], feat: dict[str, Any]) -> int:
    """0–100 score: higher = better immediate entry (closer to anchor, in reach)."""
    score = 50
    if signal.get("within_reach") is True:
        score += 25
    elif signal.get("within_reach") is False:
        score -= 30

    dist = float(signal.get("distance_atr") or 0)
    if dist <= 0.25:
        score += 20
    elif dist <= 0.75:
        score += 10
    elif dist > 2.0:
        score -= 15

    if signal.get("entry_mode") == "market":
        score += 10

    side = signal.get("side", "BUY")
    price = float(feat.get("price") or signal.get("market_price") or 0)
    anchor = signal.get("entry_anchor_price")
    if price > 0 and anchor is not None:
        atr = float(feat.get("atr") or price * 0.001) or price * 0.001
        align = abs(float(anchor) - price) / atr if atr > 0 else 99
        if align <= 0.5:
            score += 15
        elif align <= 1.0:
            score += 5

    momentum = int((signal.get("confidence_tree") or {}).get("momentum_engine", 50))
    structure = int((signal.get("confidence_tree") or {}).get("structure_engine", 50))
    score += int((momentum - 50) * 0.1)
    score += int((structure - 50) * 0.15)

    if side == "BUY" and feat.get("m5_trend") == "bullish":
        score += 5
    if side == "SELL" and feat.get("m5_trend") == "bearish":
        score += 5

    return int(min(99, max(0, score)))


def _refresh_strategy_levels(
    signal: dict[str, Any],
    feat: dict[str, Any],
    ctx: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Re-pin entry/SL/TP to the latest bar."""
    if strategy_entries_enabled(config):
        levels = pin_strategy_entry(
            signal["setup_type"],
            signal["side"],
            feat,
            ctx,
            config,
            signal["symbol"],
        )
        out = dict(signal)
        for key in (
            "entry", "sl", "tp1", "tp2", "entry_mode", "order_type",
            "entry_anchor", "entry_anchor_price", "entry_reason",
            "market_price", "distance_atr", "within_reach",
            "spread_pct_of_tp",
        ):
            if key in levels:
                out[key] = levels[key]
        return out
    return refresh_entry_levels(signal, feat)


def refine_candidates(
    candidates: list[dict[str, Any]],
    features_data: dict[str, Any],
    context_data: dict[str, Any],
    config: dict[str, Any],
    *,
    positions: list[dict[str, Any]] | None = None,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """
    Post-process decision-engine candidates:
    - Drop symbols at position capacity (optional)
    - Refresh entry levels each cycle
    - Score entry quality; drop unreachable limits (optional)
    """
    log = logger or logging.getLogger("entry_pipeline")
    se_cfg = _strategy_entries_cfg(config)
    positions = positions or []
    reject_unreachable = bool(se_cfg.get("reject_unreachable_entries", True))
    skip_capacity = bool(se_cfg.get("skip_at_capacity_in_candidates", True))
    refresh = bool(se_cfg.get("refresh_each_cycle", True))

    prune_stale()

    refined: list[dict[str, Any]] = []
    for signal in candidates:
        symbol = signal["symbol"]
        feat = (features_data.get("symbols") or {}).get(symbol) or {}
        ctx = (context_data.get("symbols") or {}).get(symbol) or signal.get("market_context") or {}

        if skip_capacity and not symbol_capacity_available(config, symbol, positions):
            log.info("Entry pipeline skip %s — position capacity full", symbol)
            continue

        if refresh and feat:
            signal = _refresh_strategy_levels(signal, feat, ctx, config)

        quality = _entry_quality(signal, feat)
        pipeline = {
            "entry_quality": quality,
            "within_reach": signal.get("within_reach"),
            "distance_atr": signal.get("distance_atr"),
            "entry_mode": signal.get("entry_mode"),
            "status": "ready",
        }

        # ---- ADAPTIVE EXIT: SPREAD / TP RATIO GATE (2026-07-22) ---------
        # Reject trade when spread > 25% of calculated TP. This prevents entries
        # when spread cost erodes too much of the target.
        if adaptive_exit_enabled(config):
            _sp = signal.get("spread_pct_of_tp", 0.0)
            if _sp > 0.25:
                pipeline["status"] = "spread_too_wide"
                log.info(
                    "Entry pipeline drop %s %s — spread/TP ratio %.1f%% > 25%%",
                    symbol, signal.get("side"), _sp * 100,
                )
                continue

        if signal.get("within_reach") is False and reject_unreachable:
            pipeline["status"] = "unreachable"
            log.info(
                "Entry pipeline drop %s %s — anchor %.5f too far (%.2f ATR)",
                symbol,
                signal.get("side"),
                float(signal.get("entry_anchor_price") or signal.get("entry") or 0),
                float(signal.get("distance_atr") or 0),
            )
            continue

        signal["entry_quality"] = quality
        signal["entry_pipeline"] = pipeline
        refined.append(signal)

    refined.sort(
        key=lambda s: (s.get("entry_quality", 0), s.get("confidence", 0)),
        reverse=True,
    )
    log.info(
        "Entry pipeline: %d in → %d refined (reject_unreachable=%s)",
        len(candidates),
        len(refined),
        reject_unreachable,
    )
    return refined