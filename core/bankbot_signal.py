"""Bankbot MA crossover signal generator.

Reimplements the TradingView bankbot strategy in Python:
  - SMMA (Smoothed Moving Average) on close and open series
  - Crossover/crossunder detection
  - ATR volatility filter
  - Alternate resolution support (e.g. M5 * 3 = M15)

Signals feed into the bot's existing pipeline (evaluation -> verifier -> execution).
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

from core.utils import read_json_state, utc_now_iso


# Per-symbol cooldown tracking to prevent duplicate signals for the same crossover event
_last_signal: dict[str, dict[str, Any]] = {}  # symbol -> {side, timestamp, close_smma, open_smma}


def _current_session_utc() -> str:
    """Derive trading session label from current UTC hour."""
    h = datetime.datetime.now(datetime.timezone.utc).hour
    if 0 <= h < 7:
        return "tokyo"
    if 7 <= h < 13:
        return "london_open"
    if 13 <= h < 16:
        return "overlap_london_ny"
    if 16 <= h < 17:
        return "new_york"
    if 17 <= h < 21:
        return "new_york_late"
    return "sydney"


def bankbot_enabled(config: dict[str, Any]) -> bool:
    """Check if bankbot signal source is enabled in config."""
    return bool((config.get("bankbot") or {}).get("enabled", False))


def _smma(values: list[float], period: int) -> list[float]:
    """Smoothed Moving Average (same as Pine Script's SMMA / RMA)."""
    if not values or period <= 0:
        return []
    result: list[float] = []
    # First value = SMA
    if len(values) >= period:
        result.append(sum(values[:period]) / period)
    else:
        return []
    # Subsequent values = (prev * (period - 1) + current) / period
    for i in range(period, len(values)):
        prev = result[-1]
        result.append((prev * (period - 1) + values[i]) / period)
    return result


def _atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    """Average True Range from candle data."""
    if len(candles) < period + 1:
        return 0.0
    tr_values: list[float] = []
    for i in range(1, len(candles)):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        prev_close = float(candles[i - 1]["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)
    if len(tr_values) < period:
        return 0.0
    return sum(tr_values[-period:]) / period


def generate_bankbot_signals(
    config: dict[str, Any],
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """Generate candidate signals from bankbot MA crossover strategy.

    Reads M5 candles from latest_candles.json, computes SMMA crossover,
    and produces signals in the same format as the DecisionEngine.
    Includes per-symbol cooldown to prevent duplicate signals for the
    same crossover event. Includes minimal market_context for session.

    Returns a list of candidate signal dicts, ready to merge into the pipeline.
    """
    global _last_signal
    log = logger or logging.getLogger("bankbot_signal")
    if not bankbot_enabled(config):
        return []

    bb_cfg = config.get("bankbot", {})
    period = int(bb_cfg.get("period", 8))
    atr_threshold = float(bb_cfg.get("atr_threshold", 0.0005))
    min_confidence = int(bb_cfg.get("min_confidence", 50))
    cooldown_cycles = int(bb_cfg.get("cooldown_cycles", 3))  # Skip if same crossover was signaled in last N cycles
    symbols = list(bb_cfg.get("symbols") or [])

    candles_data = read_json_state("latest_candles.json", default={})
    symbol_candles = candles_data.get("symbols", {})

    # If no explicit symbol list, use all symbols with data
    if not symbols:
        symbols = list(symbol_candles.keys())

    candidates: list[dict[str, Any]] = []

    for symbol in symbols:
        sd = symbol_candles.get(symbol)
        if not sd:
            log.debug("Bankbot skip %s — no candle data", symbol)
            continue

        m5_candles = sd.get("M5", [])
        if len(m5_candles) < period + 15:
            log.debug("Bankbot skip %s — insufficient M5 candles (%d < %d)", symbol, len(m5_candles), period + 15)
            continue

        # Compute ATR for volatility filter
        atr_value = _atr(m5_candles)
        current_price = float(m5_candles[-1]["close"])
        atr_ratio = atr_value / current_price if current_price > 0 else 0

        # ATR threshold check
        if atr_ratio < atr_threshold:
            log.debug("Bankbot skip %s — ATR ratio %.6f < %.6f", symbol, atr_ratio, atr_threshold)
            continue

        # Extract close and open series
        close_values = [float(c["close"]) for c in m5_candles]
        open_values = [float(c["open"]) for c in m5_candles]

        # Compute SMMA
        close_smma = _smma(close_values, period)
        open_smma = _smma(open_values, period)

        if len(close_smma) < 3 or len(open_smma) < 3:
            continue

        # Detect crossover/crossunder on the last 2 SMMA values
        # Crossover: close_smma[-2] <= open_smma[-2] AND close_smma[-1] > open_smma[-1]
        # Crossunder: close_smma[-2] >= open_smma[-2] AND close_smma[-1] < open_smma[-1]
        prev_close_smma = close_smma[-2]
        prev_open_smma = open_smma[-2]
        curr_close_smma = close_smma[-1]
        curr_open_smma = open_smma[-1]

        # Also check -3 for the previous bar's crossover (sometimes signals straddle)
        if len(close_smma) >= 4:
            prev2_close_smma = close_smma[-3]
            prev2_open_smma = open_smma[-3]
        else:
            prev2_close_smma = prev_close_smma
            prev2_open_smma = prev_open_smma

        is_buy = False
        is_sell = False
        reason = ""

        # Current bar crossover
        if prev_close_smma <= prev_open_smma and curr_close_smma > curr_open_smma:
            is_buy = True
            reason = "Bankbot BUY — SMMA crossover (close crossed above open)"
        elif prev_close_smma >= prev_open_smma and curr_close_smma < curr_open_smma:
            is_sell = True
            reason = "Bankbot SELL — SMMA crossunder (close crossed below open)"
        # Previous bar crossover (straddle)
        elif prev2_close_smma <= prev2_open_smma and prev_close_smma > prev_open_smma:
            is_buy = True
            reason = "Bankbot BUY — SMMA crossover (prior bar)"
        elif prev2_close_smma >= prev2_open_smma and prev_close_smma < prev_open_smma:
            is_sell = True
            reason = "Bankbot SELL — SMMA crossunder (prior bar)"

        if not is_buy and not is_sell:
            # Not crossed — clear last_signal so next crossover fires fresh
            if symbol in _last_signal:
                del _last_signal[symbol]
            continue

        side = "BUY" if is_buy else "SELL"

        # Cooldown check: skip if same symbol+side crossover was signaled recently
        prev = _last_signal.get(symbol)
        if prev and prev.get("side") == side:
            # Same direction signal — count as duplicate if within cooldown
            now = datetime.datetime.now(datetime.timezone.utc)
            prev_ts = prev.get("timestamp")
            if prev_ts and (now - prev_ts).total_seconds() < cooldown_cycles * 15 * 2:
                log.debug("Bankbot skip %s %s — cooldown active", symbol, side)
                continue

        # Record this signal for cooldown tracking
        _last_signal[symbol] = {
            "side": side,
            "timestamp": datetime.datetime.now(datetime.timezone.utc),
            "close_smma": curr_close_smma,
            "open_smma": curr_open_smma,
        }

        # Create minimal market_context for pipeline compatibility
        session = _current_session_utc()
        market_context = {
            "session": session,
            "regime": "unknown",
            "market_regime": {"primary": "unknown"},
        }

        # Compute confidence from ATR ratio (higher ATR = more confident)
        atr_confidence = min(85, int(50 + atr_ratio / 0.001 * 15))
        confidence = max(min_confidence, min(99, atr_confidence))

        # Simple SL/TP: 1.5 ATR risk, 1:1.5 R:R
        risk_dist = atr_value * 1.5
        reward_dist = risk_dist * 1.5

        if is_buy:
            sl = round(current_price - risk_dist, 5)
            tp1 = round(current_price + reward_dist, 5)
        else:
            sl = round(current_price + risk_dist, 5)
            tp1 = round(current_price - reward_dist, 5)

        signal = {
            "signal_id": str(uuid.uuid4()),
            "symbol": symbol,
            "side": side,
            "setup_type": "ma_crossover",
            "entry": round(current_price, 5),
            "sl": sl,
            "tp1": tp1,
            "tp2": round(current_price + reward_dist * 2.5, 5) if is_buy else round(current_price - reward_dist * 2.5, 5),
            "entry_mode": "market",
            "order_type": "market",
            "within_reach": True,
            "confidence": confidence,
            "market_context": market_context,
            "reason": reason,
            "reasons": [reason, f"ATR ratio: {atr_ratio:.6f}"],
            "source": "bankbot",
            "bankbot_params": {
                "period": period,
                "close_smma_prev": round(prev_close_smma, 5),
                "close_smma_curr": round(curr_close_smma, 5),
                "open_smma_prev": round(prev_open_smma, 5),
                "open_smma_curr": round(curr_open_smma, 5),
                "atr_ratio": round(atr_ratio, 6),
            },
            "created_at": utc_now_iso(),
        }
        candidates.append(signal)
        log.info(
            "Bankbot %s %s %s conf=%d ATR=%.6f",
            symbol, side, "ma_crossover", confidence, atr_ratio,
        )

    return candidates
