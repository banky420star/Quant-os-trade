"""ATR volatility-expansion live signal generator.

Stream name: "atr_expansion". Reads M5 candles from latest_candles.json,
emits DecisionEngine-shape candidates when ATR just expanded AND price broke
out of the N-bar channel.
"""

from __future__ import annotations

import logging
from typing import Any

from strategies.atr_expansion import AtrExpansionParams, detect_signals
from core.donchian_signal import _cooldown_hit, _stamp_cooldown
from core.utils import read_json_state, utc_now_iso


def atr_expansion_enabled(config: dict[str, Any]) -> bool:
    return bool((config.get("strategies") or {}).get("diversification") or {}).get(
        "atr_expansion_enabled", False
    )


def atr_expansion_config(config: dict[str, Any]) -> AtrExpansionParams:
    cfg = (config.get("strategies") or {}).get("diversification") or {}
    ax = cfg.get("atr_expansion") or {}
    return AtrExpansionParams(
        breakout_len=int(ax.get("breakout_len", 14)),
        atr_len=int(ax.get("atr_len", 14)),
        atr_ratio_lookback=int(ax.get("atr_ratio_lookback", 20)),
        expansion_mult=float(ax.get("expansion_mult", 1.2)),
        rr=float(ax.get("rr", 2.0)),
        sl_atr=float(ax.get("sl_atr", 1.0)),
    )


def generate_atr_expansion_signals(
    config: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
    _cooldown_state: dict[str, dict[str, Any]] | None = None,
    cooldown_seconds: int = 180,
) -> list[dict[str, Any]]:
    """Emit ATR-expansion breakout candidates."""
    log = logger or logging.getLogger("atr_expansion_signal")
    if not atr_expansion_enabled(config):
        return []

    params = atr_expansion_config(config)
    div = (config.get("strategies") or {}).get("diversification") or {}
    overrides = list(div.get("atr_expansion_symbols") or [])
    cooldown_seconds = int(div.get("cooldown_seconds", cooldown_seconds))

    candles_data = read_json_state("latest_candles.json", default={})
    symbol_candles = candles_data.get("symbols", {})
    symbols = overrides or list(symbol_candles.keys())

    out: list[dict[str, Any]] = []
    import uuid
    for symbol in symbols:
        sd = symbol_candles.get(symbol)
        if not sd:
            continue
        m5 = sd.get("M5", [])
        if len(m5) < params.atr_ratio_lookback + 30:
            continue
        try:
            import pandas as pd
            df = pd.DataFrame(m5).astype({"close": float, "open": float, "high": float, "low": float})
        except Exception as exc:
            log.warning("ATR-expansion skip %s — DF build failed: %s", symbol, exc)
            continue
        try:
            long_bars, short_bars = detect_signals(df, params)
        except Exception as exc:
            log.warning("ATR-expansion skip %s — detect_signals failed: %s", symbol, exc)
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
                from strategies.atr_expansion import atr_series
                atr = float(atr_series(df, params.atr_len).iloc[-1])
            except Exception:
                atr = max(price * 0.001, 1e-5)

        sl_dist = params.sl_atr * atr
        reward_dist = sl_dist * params.rr
        if side == "BUY":
            sl = round(price - sl_dist, 5)
            tp1 = round(price + reward_dist, 5)
            tp2 = round(price + reward_dist * 1.5, 5)
        else:
            sl = round(price + sl_dist, 5)
            tp1 = round(price - reward_dist, 5)
            tp2 = round(price - reward_dist * 1.5, 5)

        atr_ratio = float(feat.get("atr_ratio") or (atr / max(price, 1e-5)))
        confidence = min(90, int(60 + atr_ratio * 1000))

        candidate = {
            "signal_id": str(uuid.uuid4()),
            "symbol": symbol,
            "side": side,
            "setup_type": "atr_expansion",
            "entry": round(price, 5),
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "entry_mode": "market",
            "order_type": "market",
            "within_reach": True,
            "confidence": confidence,
            "risk_parity_stream": "atr_expansion",
            "market_context": {
                "regime": "expanding" if atr_ratio > 0.002 else "normal",
                "m5_trend": feat.get("m5_trend", "neutral"),
                "session": "unknown",
                "market_regime": {"primary": "expansion"},
            },
            "reason": (f"ATR expansion breakout — ATR ratio {atr_ratio:.5f} > "
                       f"{params.expansion_mult}× baseline, channel broken {side}"),
            "reasons": [f"ATR ratio expansion ({atr_ratio:.5f})",
                        f"Channel breakout ({params.breakout_len}-bar)",
                        f"SL {params.sl_atr}×ATR, TP {params.rr}R"],
            "source": "atr_expansion",
            "atr_expansion_params": params.as_dict(),
            "created_at": utc_now_iso(),
        }
        out.append(candidate)
        _stamp_cooldown(_cooldown_state, symbol, side, utc_now_iso())
        log.info(
            "ATR-expansion %s %s %s conf=%d",
            symbol, side, "atr_expansion", confidence,
        )
    return out
