"""Bounded per-symbol adaptation from sampled ticks and closed outcomes.

This module is deliberately a *shadow* learner.  It observes the active
execution ledger plus the latest sampled tick state and writes a proposal for
operator/replay review.  It never changes config, places orders, or bypasses
verifier/risk gates.

The important separation is:

    ticks -> observations -> proposal -> replay/paper validation -> approval

A proposal is evidence about a symbol, not proof of a permanent edge.  The
bounds and minimum sample gate make the first implementation useful without
turning one noisy session into a live risk change.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from core.trade_history import read_closed_trades, trade_history_filename
from core.utils import read_json_state, utc_now_iso, write_json_state

STATE_FILE = "adaptive_symbol_learning.json"
TICK_STATE_FILE = "adaptive_tick_state.json"

_LAST_TICK_WRITE = 0.0


def _cfg(config: dict[str, Any]) -> dict[str, Any]:
    learning = config.get("self_learning") or {}
    return learning.get("adaptive_symbol_learning") or {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _r(value: float, digits: int = 4) -> float:
    return round(value, digits)


def _result_r(trade: dict[str, Any]) -> float | None:
    """Return a realized R value; never mistake USD PnL for R.

    Closed rows without an explicit R-multiple are excluded from sizing
    evidence. A raw ``pnl`` value is account-currency dependent and cannot be
    compared safely across symbols or account sizes.
    """
    r = trade.get("r_multiple")
    if r is None:
        return None
    try:
        value = float(r)
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def _symbol_stats(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        symbol = str(trade.get("symbol") or "").strip()
        if symbol:
            buckets[symbol].append(trade)

    output: dict[str, dict[str, Any]] = {}
    for symbol, rows in buckets.items():
        resolved = [(row, _result_r(row)) for row in rows]
        resolved = [(row, r) for row, r in resolved if r is not None]
        rs = [r for _, r in resolved]
        wins = [row for row, r in resolved if r > 0]
        losses = [row for row, r in resolved if r < 0]
        win_rs = [_result_r(row) for row in wins]
        loss_rs = [_result_r(row) for row in losses]
        win_rs = [r for r in win_rs if r is not None]
        loss_rs = [r for r in loss_rs if r is not None]
        def _confidence(trade: dict[str, Any]) -> float:
            meta = trade.get("signal_meta")
            if not isinstance(meta, dict):
                meta = {}
            return _f(meta.get("confidence", trade.get("confidence")), 50.0)

        win_conf = [_confidence(row) for row in wins]
        loss_conf = [_confidence(row) for row in losses]
        hold_values = [_f(row.get("hold_seconds")) for row in wins if _f(row.get("hold_seconds")) > 0]
        timing_scores = [_f(row.get("entry_timing_score")) for row in rows if row.get("entry_timing_score") is not None]
        mistakes: dict[str, int] = {}
        for row in rows:
            raw_mistakes = row.get("mistake_categories") or []
            if isinstance(raw_mistakes, str):
                raw_mistakes = [raw_mistakes]
            if not isinstance(raw_mistakes, (list, tuple, set)):
                raw_mistakes = []
            for mistake in raw_mistakes:
                mistakes[str(mistake)] = mistakes.get(str(mistake), 0) + 1
        output[symbol] = {
            "sample_size": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": _r(100.0 * len(wins) / len(rows), 1) if rows else 0.0,
            "expectancy_r": _r(sum(rs) / len(rs), 4) if rs else 0.0,
            "winner_avg_r": _r(sum(win_rs) / len(win_rs), 4) if win_rs else 0.0,
            "loser_avg_r": _r(sum(loss_rs) / len(loss_rs), 4) if loss_rs else 0.0,
            "winner_avg_confidence": _r(sum(win_conf) / len(win_conf), 2) if win_conf else None,
            "loser_avg_confidence": _r(sum(loss_conf) / len(loss_conf), 2) if loss_conf else None,
            "winner_avg_hold_seconds": _r(sum(hold_values) / len(hold_values), 1) if hold_values else None,
            "entry_timing_score": _r(sum(timing_scores) / len(timing_scores), 2) if timing_scores else None,
            "mistakes": mistakes,
        }
    return output


def _bounded(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def build_symbol_proposals(
    config: dict[str, Any],
    trades: list[dict[str, Any]],
    tick_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build bounded per-symbol proposals from closed outcomes and tick state."""
    cfg = _cfg(config)
    min_trades = max(1, int(cfg.get("min_trades", 20)))
    base_conf = _f((config.get("signals") or {}).get("min_confidence"), 50.0)
    max_up = _f(cfg.get("max_risk_multiplier_up"), 1.10)
    max_down = _f(cfg.get("max_risk_multiplier_down"), 0.50)
    tick_state = tick_state or {}
    stats = _symbol_stats(trades)
    proposals: dict[str, dict[str, Any]] = {}

    for symbol, row in stats.items():
        n = int(row["sample_size"])
        tick = (tick_state.get("symbols") or {}).get(symbol) or {}
        enough = n >= min_trades
        exp = _f(row.get("expectancy_r"))

        # Confidence is a small, reviewable threshold suggestion.  It is based
        # on separation between winner and loser confidence, not raw win rate.
        winner_conf = row.get("winner_avg_confidence")
        loser_conf = row.get("loser_avg_confidence")
        if enough and winner_conf is not None and loser_conf is not None:
            separation = _f(winner_conf) - _f(loser_conf)
            confidence_delta = _bounded(round(separation / 5.0), -5.0, 5.0)
        else:
            confidence_delta = 0.0

        # Risk is never increased more than 10%, and only after positive
        # expectancy. Losing/uncertain symbols de-risk toward 0.5x.
        if not enough:
            risk_multiplier = 1.0
        elif exp > 0:
            risk_multiplier = _bounded(1.0 + exp * 0.05, 1.0, max_up)
        elif exp < 0:
            risk_multiplier = _bounded(1.0 + exp * 0.10, max_down, 1.0)
        else:
            risk_multiplier = 1.0

        # Timing uses current tick conditions only as a shadow recommendation:
        # high spread relative to ATR waits for normalization; weak volume uses
        # a short confirmation delay. It is not wired into order submission.
        atr = _f(tick.get("atr"))
        spread = _f(tick.get("spread_points"))
        point = _f(tick.get("point"), 0.01)
        spread_atr = (spread * point / atr) if atr > 0 else 0.0
        volume_ratio = _f(tick.get("volume_ratio"), 1.0)
        if spread_atr >= _f(cfg.get("high_spread_atr_ratio"), 0.20):
            timing = {"entry_delay_seconds": 15, "reason": "wait_for_spread_normalization"}
        elif volume_ratio < _f(cfg.get("weak_volume_ratio"), 0.80):
            timing = {"entry_delay_seconds": 5, "reason": "wait_for_volume_confirmation"}
        else:
            timing = {"entry_delay_seconds": 0, "reason": "normal_tick_conditions"}

        target_hold = row.get("winner_avg_hold_seconds")
        target_hold = _bounded(_f(target_hold, 300.0), 60.0, 3600.0) if enough else None
        allow_pyramiding = bool((config.get("trading") or {}).get("allow_pyramiding", False))
        pyramid_layers = 1 if enough and allow_pyramiding and exp > 0 else 0

        proposals[symbol] = {
            "status": "shadow_candidate" if enough else "insufficient_data",
            "sample_size": n,
            "stats": row,
            "proposed": {
                "min_confidence": _r(_bounded(base_conf + confidence_delta, base_conf - 5, base_conf + 5), 1),
                "risk_multiplier": _r(risk_multiplier, 4),
                "entry_delay_seconds": timing["entry_delay_seconds"],
                "target_hold_seconds": _r(target_hold, 1) if target_hold is not None else None,
                "max_pyramid_layers": pyramid_layers,
            },
            "timing_reason": timing["reason"],
            "tick_snapshot": tick,
            "safety": {
                "live_config_changed": False,
                "orders_placed": 0,
                "risk_multiplier_max": max_up,
                "risk_multiplier_min": max_down,
            },
        }

    return {
        "timestamp": utc_now_iso(),
        "mode": str(cfg.get("mode") or "shadow"),
        "enabled": bool(cfg.get("enabled", True)),
        "min_trades": min_trades,
        "source_ledger": trade_history_filename(config),
        "symbols": proposals,
        "note": "Shadow proposals require replay/paper validation and explicit approval before any live application.",
    }


def run_adaptive_symbol_learning(config: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    """Refresh the per-symbol shadow proposal ledger."""
    cfg = _cfg(config)
    if not cfg.get("enabled", True):
        output = {"timestamp": utc_now_iso(), "mode": "disabled", "enabled": False, "symbols": {}}
    else:
        trades = read_closed_trades(config, limit=max(1, int(cfg.get("lookback", 200))))
        tick_state = read_json_state(TICK_STATE_FILE, default={}) or {}
        output = build_symbol_proposals(config, trades, tick_state)
    if persist:
        write_json_state(STATE_FILE, output)
    return output


def record_tick_snapshot(
    config: dict[str, Any],
    prices: dict[str, dict[str, Any]],
    features: dict[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Sample current tick conditions for later shadow timing proposals."""
    global _LAST_TICK_WRITE
    cfg = _cfg(config)
    if not cfg.get("enabled", True):
        return None
    now = time.monotonic() if now is None else now
    interval = max(1.0, _f(cfg.get("tick_sample_seconds"), 5.0))
    if _LAST_TICK_WRITE and now - _LAST_TICK_WRITE < interval:
        return None
    _LAST_TICK_WRITE = now

    previous = read_json_state(TICK_STATE_FILE, default={}) or {}
    symbols = dict(previous.get("symbols") or {})
    feature_symbols = (features.get("symbols") or {}) if isinstance(features, dict) else {}
    for symbol, quote in prices.items():
        feat = feature_symbols.get(symbol) or {}
        mid = _f(quote.get("mid") or feat.get("price"))
        if mid <= 0:
            continue
        old = symbols.get(symbol) or {}
        old_mid = _f(old.get("price"), mid)
        atr = _f(feat.get("atr") or feat.get("atr_14"))
        move_atr = abs(mid - old_mid) / atr if atr > 0 and old.get("price") else 0.0
        alpha = 0.2
        symbols[symbol] = {
            "last_tick_at": utc_now_iso(),
            "price": mid,
            "atr": atr,
            "point": _f(feat.get("point"), 0.01),
            "spread_points": _f(quote.get("spread_points") or feat.get("spread_points")),
            "volume_ratio": _f(feat.get("volume_ratio"), 1.0),
            "move_atr_ema": _r(alpha * move_atr + (1 - alpha) * _f(old.get("move_atr_ema")), 6),
            "spread_points_ema": _r(alpha * _f(quote.get("spread_points") or feat.get("spread_points")) + (1 - alpha) * _f(old.get("spread_points_ema")), 4),
            "tick_count": int(old.get("tick_count") or 0) + 1,
        }
    output = {"timestamp": utc_now_iso(), "symbols": symbols}
    write_json_state(TICK_STATE_FILE, output)
    return output
