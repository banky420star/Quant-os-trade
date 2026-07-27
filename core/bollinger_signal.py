"""Bollinger reversion live signal generator.

Reads M5 candles from state/latest_candles.json, detects 2σ Bollinger band
mean-reversion entries, and emits DecisionEngine-shaped candidates with
`source="bollinger_reversion"`. Restricted to FX pairs only — there is NO
override path; to extend to non-FX symbols, edit `strategies.portfolio.FX_SYMBOLS`.

Stream name: "bollinger_reversion"
"""

from __future__ import annotations

import logging
from typing import Any

from strategies.bollinger_reversion import BollingerParams, detect_signals
from strategies.portfolio import FX_SYMBOLS
from core.donchian_signal import _cooldown_hit, _stamp_cooldown
from core.utils import read_json_state, utc_now_iso


def bollinger_enabled(config: dict[str, Any]) -> bool:
    return bool(((config.get("strategies") or {}).get("diversification") or {}).get(
        "bollinger_enabled", False
    ))


def bollinger_config(config: dict[str, Any]) -> BollingerParams:
    cfg = (config.get("strategies") or {}).get("diversification") or {}
    bl = cfg.get("bollinger") or {}
    return BollingerParams(
        period=int(bl.get("period", 20)),
        std_mult=float(bl.get("std_mult", 2.0)),
        atr_len=int(bl.get("atr_len", 14)),
        atr_threshold=float(bl.get("atr_threshold", 0.0005)),
        rr=float(bl.get("rr", 0.8)),
        sl_atr=float(bl.get("sl_atr", 1.0)),
        require_stoch=bool(bl.get("require_stoch", True)),
    )


def generate_bollinger_signals(
    config: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
    _cooldown_state: dict[str, dict[str, Any]] | None = None,
    cooldown_seconds: int = 180,
) -> list[dict[str, Any]]:
    """Emit Bollinger reversion candidates — FX-only."""
    log = logger or logging.getLogger("bollinger_signal")
    if not bollinger_enabled(config):
        return []

    params = bollinger_config(config)
    div = (config.get("strategies") or {}).get("diversification") or {}
    cooldown_seconds = int(div.get("cooldown_seconds", cooldown_seconds))

    # Bollinger reversion is FX-ONLY. There is intentionally NO override path:
    # allowing expansion to non-FX would silently violate the strategy thesis
    # and erode diversification. To extend to non-FX, edit FX_SYMBOLS.
    symbols = list(FX_SYMBOLS)

    candles_data = read_json_state("latest_candles.json", default={})
    symbol_candles = candles_data.get("symbols", {})

    out: list[dict[str, Any]] = []
    import uuid
    for symbol in symbols:
        sd = symbol_candles.get(symbol)
        if not sd:
            continue
        m5 = sd.get("M5", [])
        if len(m5) < params.period + 30:
            continue
        try:
            import pandas as pd
            df = pd.DataFrame(m5).astype({"close": float, "open": float, "high": float, "low": float})
        except Exception as exc:
            log.warning("Bollinger skip %s — DF build failed: %s", symbol, exc)
            continue
        try:
            long_bars, short_bars = detect_signals(df, params)
        except Exception as exc:
            log.warning("Bollinger skip %s — detect_signals failed: %s", symbol, exc)
            continue

        if not (len(long_bars) or len(short_bars)):
            continue
        last_long = int(long_bars[-1]) if len(long_bars) else -1
        last_short = int(short_bars[-1]) if len(short_bars) else -1
        if last_long > last_short and last_long > 0:
            side = "BUY"
        elif last_short > 0:
            side = "SELL"
        else:
            continue

        # Per-symbol + side cooldown — prevents re-fire across cycles.
        prev = (_cooldown_state or {}).get(symbol)
        if _cooldown_hit(symbol, side, prev, cooldown_seconds):
            continue

        feat = read_json_state("features.json", default={}).get("symbols", {}).get(symbol, {})
        price = float(feat.get("price") or m5[-1].get("close") or 0.0)
        atr = float(feat.get("atr") or 0.0)
        if atr <= 0:
            try:
                from strategies.bollinger_reversion import atr_series
                atr = float(atr_series(df, params.atr_len).iloc[-1])
            except Exception:
                atr = max(price * 0.001, 1e-5)

        sl_dist = params.sl_atr * atr
        reward_dist = sl_dist * params.rr
        if side == "BUY":
            sl = round(price - sl_dist, 5)
            tp1 = round(price + reward_dist, 5)
            tp2 = round(price + reward_dist * 1.4, 5)
        else:
            sl = round(price + sl_dist, 5)
            tp1 = round(price - reward_dist, 5)
            tp2 = round(price - reward_dist * 1.4, 5)

        confidence = 72
        candidate = {
            "signal_id": str(uuid.uuid4()),
            "symbol": symbol,
            "side": side,
            "setup_type": "bollinger_reversion",
            "entry": round(price, 5),
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "entry_mode": "market",
            "order_type": "market",
            "within_reach": True,
            "confidence": confidence,
            "risk_parity_stream": "bollinger_reversion",
            "market_context": {
                "regime": feat.get("volatility_regime", "ranging"),
                "m5_trend": feat.get("m5_trend", "neutral"),
                "session": "unknown",
                "market_regime": {"primary": "range"},
            },
            "reason": (f"Bollinger reversion: close crossed off {params.std_mult}σ "
                       f"{'lower' if side == 'BUY' else 'upper'} band"),
            "reasons": [f"2σ Bollinger reversion ({side})",
                        f"SL {params.sl_atr}×ATR, TP {params.rr}R toward midline",
                        "FX-only stream"],
            "source": "bollinger_reversion",
            "bollinger_params": params.as_dict(),
            "created_at": utc_now_iso(),
        }
        out.append(candidate)
        _stamp_cooldown(_cooldown_state, symbol, side, utc_now_iso())
        log.info("Bollinger %s %s %s conf=%d", symbol, side, "bollinger_reversion", confidence)
    return out
