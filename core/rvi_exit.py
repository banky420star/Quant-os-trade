"""Confirmed M15 Relative Vigor Index crossover exits.

This module is intentionally exit-only and fail-closed. It never opens trades.
A close is eligible only when:

* trading.rvi_exit.enabled is true;
* the symbol has enough M15 candles in latest_candles.json;
* the last completed M15 bar is fresh enough;
* the RVI main/signal lines show a newly confirmed crossover against the
  position direction on completed bars.

BUY closes on a bearish crossover (RVI main crosses below signal).
SELL closes on a bullish crossover (RVI main crosses above signal).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.mt5_broker import MT5Broker
from core.utils import read_json_state, utc_now_iso


def _as_float(row: dict[str, Any], key: str) -> float:
    return float(row.get(key, 0.0) or 0.0)


def _bar_time(row: dict[str, Any]) -> datetime | None:
    raw = row.get("time") or row.get("timestamp")
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _weighted_component(values: list[float], i: int) -> float:
    return (values[i] + 2.0 * values[i - 1] + 2.0 * values[i - 2] + values[i - 3]) / 6.0


def rvi_series(bars: list[dict[str, Any]], period: int = 10) -> tuple[list[float], list[float]]:
    """Return aligned RVI main and signal series for completed OHLC bars."""
    if period < 2 or len(bars) < period + 6:
        return [], []

    co = [_as_float(b, "close") - _as_float(b, "open") for b in bars]
    hl = [max(abs(_as_float(b, "high") - _as_float(b, "low")), 1e-12) for b in bars]

    num4: list[float | None] = [None] * len(bars)
    den4: list[float | None] = [None] * len(bars)
    for i in range(3, len(bars)):
        num4[i] = _weighted_component(co, i)
        den4[i] = _weighted_component(hl, i)

    main_by_index: list[float | None] = [None] * len(bars)
    for i in range(period + 2, len(bars)):
        nums = [num4[j] for j in range(i - period + 1, i + 1)]
        dens = [den4[j] for j in range(i - period + 1, i + 1)]
        if any(v is None for v in nums) or any(v is None for v in dens):
            continue
        numerator = sum(float(v) for v in nums)
        denominator = sum(float(v) for v in dens)
        if abs(denominator) <= 1e-12:
            continue
        main_by_index[i] = numerator / denominator

    main: list[float] = []
    signal: list[float] = []
    for i in range(period + 5, len(bars)):
        vals = [main_by_index[i - k] for k in range(4)]
        if any(v is None for v in vals):
            continue
        cur = float(vals[0])
        sig = (cur + 2.0 * float(vals[1]) + 2.0 * float(vals[2]) + float(vals[3])) / 6.0
        main.append(cur)
        signal.append(sig)
    return main, signal


def confirmed_cross(side: str, bars: list[dict[str, Any]], period: int = 10) -> tuple[bool, str, dict[str, float]]:
    """Detect a newly confirmed opposite RVI crossover on completed bars."""
    main, signal = rvi_series(bars, period=period)
    if len(main) < 2 or len(signal) < 2:
        return False, "insufficient_rvi_history", {}

    prev_main, cur_main = main[-2], main[-1]
    prev_sig, cur_sig = signal[-2], signal[-1]
    side = str(side).upper()

    if side == "BUY":
        crossed = prev_main >= prev_sig and cur_main < cur_sig
        reason = "rvi_m15_bearish_cross" if crossed else "no_bearish_cross"
    elif side == "SELL":
        crossed = prev_main <= prev_sig and cur_main > cur_sig
        reason = "rvi_m15_bullish_cross" if crossed else "no_bullish_cross"
    else:
        return False, "unknown_side", {}

    return crossed, reason, {
        "prev_main": round(prev_main, 8),
        "prev_signal": round(prev_sig, 8),
        "main": round(cur_main, 8),
        "signal": round(cur_sig, 8),
    }


def _m15_bars(snapshot: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    symbols = snapshot.get("symbols") or {}
    row = symbols.get(symbol) or {}
    bars = row.get("M15") or row.get("m15") or []
    return list(bars) if isinstance(bars, list) else []


def evaluate_position_rvi_exit(
    config: dict[str, Any],
    position: dict[str, Any],
    candle_snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    cfg = ((config.get("trading") or {}).get("rvi_exit") or {})
    if not cfg.get("enabled", False):
        return {"close": False, "reason": "disabled"}

    symbol = str(position.get("symbol") or "")
    bars = _m15_bars(candle_snapshot, symbol)
    # Drop the newest M15 candle so crossover decisions use completed bars only.
    completed = bars[:-1] if len(bars) > 1 else []
    period = int(((cfg.get("per_symbol") or {}).get(symbol) or {}).get("period", cfg.get("period", 10)))
    minimum = period + 7
    if len(completed) < minimum:
        return {"close": False, "reason": "insufficient_m15_bars", "bars": len(completed)}

    last_time = _bar_time(completed[-1])
    if last_time is None:
        return {"close": False, "reason": "missing_m15_timestamp"}
    ref = now or datetime.now(timezone.utc)
    age = max(0.0, (ref - last_time.astimezone(timezone.utc)).total_seconds())
    max_age = float(cfg.get("max_data_age_seconds", 1200))
    if age > max_age:
        return {"close": False, "reason": "stale_m15_data", "data_age_seconds": round(age, 1)}

    crossed, reason, values = confirmed_cross(str(position.get("side") or ""), completed, period=period)
    return {
        "close": crossed,
        "reason": reason,
        "symbol": symbol,
        "period": period,
        "timeframe": "M15",
        "data_age_seconds": round(age, 1),
        **values,
    }


def manage_mt5_rvi_exits(
    config: dict[str, Any],
    positions: list[dict[str, Any]],
    logger,
) -> dict[str, Any]:
    """Close agent MT5 positions on confirmed opposite M15 RVI crossover."""
    cfg = ((config.get("trading") or {}).get("rvi_exit") or {})
    summary: dict[str, Any] = {"checked": 0, "closed": 0, "actions": [], "errors": [], "timestamp": utc_now_iso()}
    if not cfg.get("enabled", False):
        summary["skipped"] = "disabled"
        return summary
    if not (config.get("execution") or {}).get("live_trading_enabled", False):
        summary["skipped"] = "live_trading_enabled_false"
        return summary

    candles = read_json_state("latest_candles.json", default={}) or {}
    broker = MT5Broker(config, logger)
    for pos in positions:
        summary["checked"] += 1
        verdict = evaluate_position_rvi_exit(config, pos, candles)
        if not verdict.get("close"):
            continue
        result = broker.close_position(
            int(pos["ticket"]),
            str(pos["symbol"]),
            str(pos["side"]),
            float(pos.get("size") or pos.get("volume") or 0),
            reason="rvi_m15_cross",
        )
        action = {"ticket": pos.get("ticket"), "symbol": pos.get("symbol"), "side": pos.get("side"), "verdict": verdict, "result": result}
        summary["actions"].append(action)
        if result.get("success"):
            summary["closed"] += 1
            logger.info("RVI M15 exit: closed %s %s ticket=%s reason=%s", pos.get("symbol"), pos.get("side"), pos.get("ticket"), verdict.get("reason"))
        else:
            summary["errors"].append(action)
            logger.error("RVI M15 exit failed: %s ticket=%s error=%s", pos.get("symbol"), pos.get("ticket"), result.get("error"))
    return summary
