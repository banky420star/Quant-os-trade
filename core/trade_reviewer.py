"""Trade review engine (Phase 2.4).

Scores completed trades and diagnoses *why* they won/lost by combining the
trade record with optional post-exit price movement. Emits mistake categories
an LLM/ML reviewer can learn from and bounded config-change suggestions.

All detectors are defensive: missing data degrades gracefully to `None` rather
than raising, so the loop can review sparse historical trades too.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.learning_schema import MISTAKE_TYPES

_EARLY_EXIT_REASONS = {"take_profit", "tp", "partial_take_profit", "tp1", "tp_hit"}
_SL_EXIT_REASONS = {"stop_loss", "sl", "sl_hit", "break_even_stop", "break_even"}
_BAD_SESSIONS = {"off_hours", "rollover", "sydney", "unknown"}


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _side_sign(side: str) -> float:
    return 1.0 if str(side).upper() == "BUY" else -1.0


def _post_exit_excursion(
    side: str, entry: float, atr: float, post_exit_prices: list[float] | None,
) -> tuple[float, float]:
    """Return (max_favorable_atr, max_adverse_atr) of price movement after exit."""
    if not post_exit_prices or atr <= 0:
        return 0.0, 0.0
    sign = _side_sign(side)
    fav = 0.0
    adv = 0.0
    for px in post_exit_prices:
        move = (float(px) - entry) * sign
        dist = move / atr
        if dist > fav:
            fav = dist
        if dist < adv:
            adv = dist
    return round(fav, 3), round(adv, 3)


def _features(trade: dict[str, Any]) -> dict[str, Any]:
    return trade.get("features_at_entry") or trade.get("signal_meta", {}).get("features_at_entry") or {}


def detect_wrong_timeframe_alignment(trade: dict[str, Any]) -> bool:
    feat = _features(trade)
    m5 = str(feat.get("m5_trend") or "").lower()
    m15 = str(feat.get("m15_trend") or "").lower()
    if not m5 or not m15 or m5 == "none" or m15 == "none":
        return False
    opposites = {("bullish", "bearish"), ("bearish", "bullish")}
    return (m5, m15) in opposites or (m15, m5) in opposites


def detect_chop_zone(trade: dict[str, Any]) -> bool:
    feat = _features(trade)
    regime = str(trade.get("regime_primary") or "").lower()
    bb = _f(feat.get("bb_position"), -1.0)
    if regime in ("compression", "range", "ranging"):
        return True
    if 0.0 <= bb <= 1.0 and 0.35 <= bb <= 0.65:
        return True
    return False


def detect_bad_session(trade: dict[str, Any]) -> bool:
    return str(trade.get("session") or "unknown").lower() in _BAD_SESSIONS


def detect_spread_spike(
    spread_points: float, baseline_spread: float, *, max_mult: float = 2.0,
) -> bool:
    if spread_points <= 0 or baseline_spread <= 0:
        return False
    return spread_points > baseline_spread * max_mult


def detect_overtrading(recent_trades: list[dict[str, Any]], symbol: str, *, window_minutes: int = 10, cap: int = 4) -> bool:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes)
    count = 0
    for t in recent_trades:
        if t.get("symbol") != symbol:
            continue
        ts = _parse_iso(t.get("opened_at") or t.get("closed_at"))
        if ts and ts >= cutoff:
            count += 1
    return count > cap


def detect_missed_trade_after_skip(
    skip_event: dict[str, Any], post_skip_prices: list[float], *, min_move_atr: float = 0.8,
) -> dict[str, Any] | None:
    """A skip followed by a favorable move that would have reached a typical TP."""
    side = skip_event.get("side") or skip_event.get("signal_side")
    entry = _f(skip_event.get("price") or skip_event.get("anchor"))
    atr = _f(skip_event.get("atr"))
    if not side or entry <= 0 or atr <= 0 or not post_skip_prices:
        return None
    sign = _side_sign(side)
    fav = max(((float(px) - entry) * sign) / atr for px in post_skip_prices) if post_skip_prices else 0.0
    if fav >= min_move_atr:
        return {
            "mistake_type": "missed_trade_after_skip",
            "evidence": {"post_skip_max_favorable_atr": round(fav, 3)},
        }
    return None


def _score_trade(trade: dict[str, Any], post_fav_atr: float, post_adv_atr: float, mistakes: list[str]) -> dict[str, int]:
    feat = _features(trade)
    r = _f(trade.get("r_multiple"))
    mfe = _f(trade.get("mfe_R"))
    mae = _f(trade.get("mae_R"))
    dist_atr = _f((trade.get("signal_meta") or trade).get("distance_atr"))
    won = trade.get("result") == "win" or r > 0

    # Entry timing: close to anchor and the move materialized
    entry_timing = 60.0
    if dist_atr > 0:
        entry_timing = max(10.0, 90.0 - dist_atr * 60.0)
    if "entry_late" in mistakes:
        entry_timing -= 25.0
    if detect_wrong_timeframe_alignment(trade):
        entry_timing -= 20.0

    # Exit quality: fraction of MFE captured (winners) / adverse after exit (losers)
    exit_quality = 55.0
    if won and mfe > 0:
        captured = max(0.0, min(1.5, r / mfe)) if mfe else 0.0
        exit_quality = 40.0 + captured * 40.0
        if "tp_too_early" in mistakes:
            exit_quality -= 25.0
    if not won:
        exit_quality = 45.0
        if "sl_too_tight" in mistakes:
            exit_quality -= 15.0
        if "tp_too_far" in mistakes:
            exit_quality -= 10.0

    # Risk: SL appropriateness; tight SL that got stopped then reversed is bad
    risk = 60.0
    if "sl_too_tight" in mistakes:
        risk -= 25.0
    if mae and mae > -0.15 and not won:
        risk -= 15.0  # stopped on a tiny adverse wiggle

    # Trend alignment
    trend_alignment = 50.0
    if not detect_wrong_timeframe_alignment(trade):
        trend_alignment = 70.0
    else:
        trend_alignment = 25.0
    if detect_chop_zone(trade):
        trend_alignment -= 15.0

    # Execution: spread / fill
    execution = 70.0
    spread = _f(feat.get("spread_points"))
    if spread > 0:
        if spread > 80:
            execution -= 30.0
        elif spread > 40:
            execution -= 15.0
    if "spread_spike_entry" in mistakes or "spread_spike_exit" in mistakes:
        execution -= 25.0

    def clamp(x: float) -> int:
        return int(max(0.0, min(100.0, x)))

    total = round(0.25 * entry_timing + 0.30 * exit_quality + 0.20 * risk + 0.15 * trend_alignment + 0.10 * execution)
    return {
        "entry_timing_score": clamp(entry_timing),
        "exit_quality_score": clamp(exit_quality),
        "risk_score": clamp(risk),
        "trend_alignment_score": clamp(trend_alignment),
        "execution_score": clamp(execution),
        "rating_total": clamp(total),
    }


def review_trade(
    trade: dict[str, Any],
    *,
    post_exit_prices: list[float] | None = None,
    baseline_spread: float | None = None,
    recent_trades: list[dict[str, Any] | None] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a normalized review record for a completed trade."""
    cfg = config or {}
    feat = _features(trade)
    symbol = trade.get("symbol") or ""
    side = trade.get("side") or ""
    entry = _f(trade.get("entry") or trade.get("open_price"))
    exit_price = _f(trade.get("exit") or trade.get("exit_recorded") or trade.get("exit_price"))
    sl = _f(trade.get("sl") or trade.get("sl_initial"))
    tp1 = _f(trade.get("tp1"))
    atr = _f(feat.get("atr") or feat.get("atr_14"))
    if atr <= 0:
        atr = max(abs(entry) * 0.001, 0.01)
    r = _f(trade.get("r_multiple"))
    mfe = _f(trade.get("mfe_R"))
    mae = _f(trade.get("mae_R"))
    won = trade.get("result") == "win" or r > 0
    exit_reason = str(trade.get("exit_reason") or "").lower()

    post_fav, post_adv = _post_exit_excursion(side, entry, atr, post_exit_prices)

    mistakes: list[str] = []
    diagnosis: list[str] = []

    # TP too early: winner that left a meaningful move on the table
    tp_early_threshold = float((cfg.get("learning") or {}).get("tp_early_atr_threshold", 0.3))
    if won and (post_fav >= tp_early_threshold or (mfe > 0 and r > 0 and (mfe - r) >= 0.5)):
        mistakes.append("tp_too_early")
        diagnosis.append("winner exited before the full favorable move played out")

    # TP too far: price never realistically approached TP
    if tp1 > 0 and entry > 0 and mfe is not None:
        tp_r = abs(tp1 - entry) / max(abs(entry - sl) if sl > 0 else atr, 0.01)
        if mfe < 0.5 * tp_r and not won:
            mistakes.append("tp_too_far")
            diagnosis.append("target was beyond the move's reach")

    # SL too tight: stopped then price went the intended way
    if not won and exit_reason in _SL_EXIT_REASONS:
        if post_fav >= 0.3 or (mae is not None and mae > -0.2):
            mistakes.append("sl_too_tight")
            diagnosis.append("stop hit on a small adverse wiggle, then price reversed into the trade direction")

    # Entry late: entered far from the anchor / move mostly done
    dist_atr = _f((trade.get("signal_meta") or trade).get("distance_atr"))
    if dist_atr > 1.0 or (mfe is not None and mfe < 0.2 and not won):
        mistakes.append("entry_late")
        diagnosis.append("entered after most of the move had already played out")

    if detect_wrong_timeframe_alignment(trade):
        mistakes.append("wrong_timeframe_alignment")
        diagnosis.append("M5 and M15 trends disagreed at entry")

    spread = _f(feat.get("spread_points"))
    base = baseline_spread if baseline_spread else spread
    if detect_spread_spike(spread, base):
        mistakes.append("spread_spike_entry")
        diagnosis.append("spread was elevated at entry")
    if detect_spread_spike(_f(trade.get("exit_spread_points")), base):
        mistakes.append("spread_spike_exit")

    if recent_trades is not None and detect_overtrading(list(recent_trades), symbol):
        mistakes.append("overtrading")
        diagnosis.append("too many trades on the symbol within the rate window")

    if detect_chop_zone(trade):
        mistakes.append("chop_zone_entry")
        diagnosis.append("entered inside a messy/range zone")

    entry_mode = str((trade.get("signal_meta") or trade).get("entry_mode") or "")
    if entry_mode == "limit" and dist_atr > 0 and (dist_atr > 1.5 or dist_atr < 0.02):
        mistakes.append("poor_limit_distance")
        diagnosis.append("limit order placed too far from / right on top of market")

    if detect_bad_session(trade):
        mistakes.append("bad_session")
        diagnosis.append("trade taken during a low-quality session")

    scores = _score_trade(trade, post_fav, post_adv, mistakes)

    suggested_actions: list[str] = []
    suggested_config: dict[str, Any] = {}
    if "tp_too_early" in mistakes:
        suggested_actions.append("let winners run — widen TP / arm trail later / use partial TP")
        suggested_config["tp_atr_mult_delta"] = 0.1
        suggested_config["partial_tp_enabled"] = True
    if "sl_too_tight" in mistakes:
        suggested_actions.append("give the stop more room (ATR-scaled)")
        suggested_config["sl_atr_mult_delta"] = 0.1
    if "wrong_timeframe_alignment" in mistakes:
        suggested_actions.append("require higher-timeframe agreement before entry")
        suggested_config["require_htf_alignment"] = True
    if "spread_spike_entry" in mistakes:
        suggested_actions.append("tighten the spread guard")
        suggested_config["max_spread_mult_delta"] = -0.1
    if "overtrading" in mistakes:
        suggested_actions.append("lower the per-symbol trade cap")
        suggested_config["max_trades_per_10min_delta"] = -1
    if "chop_zone_entry" in mistakes:
        suggested_actions.append("skip entries inside compression/range")
        suggested_config["skip_chop_zone"] = True
    if "bad_session" in mistakes:
        suggested_actions.append("restrict trades to high-quality sessions")
        suggested_config["strict_sessions"] = True

    review = {
        "trade_id": trade.get("trade_id") or trade.get("mt5_deal"),
        "ticket": trade.get("mt5_position"),
        "symbol": symbol,
        "side": side,
        "setup": trade.get("setup") or trade.get("setup_type"),
        "entry_price": round(entry, 5) if entry else None,
        "exit_price": round(exit_price, 5) if exit_price else None,
        "sl": round(sl, 5) if sl else None,
        "tp1": round(tp1, 5) if tp1 else None,
        "duration_seconds": trade.get("hold_seconds"),
        "net_profit": _f(trade.get("pnl")),
        "r_multiple": round(r, 3) if r else None,
        "mfe_R": round(mfe, 3) if mfe else None,
        "mae_R": round(mae, 3) if mae else None,
        "post_exit_max_favorable_atr": post_fav,
        "post_exit_max_adverse_atr": post_adv,
        "mistake_categories": mistakes,
        "diagnosis": diagnosis,
        "suggested_actions": suggested_actions,
        "suggested_config_changes": suggested_config,
        **scores,
    }
    # keep only declared mistake types
    review["mistake_categories"] = [m for m in review["mistake_categories"] if m in MISTAKE_TYPES]
    return review
