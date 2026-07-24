"""Donchian breakout live signal generator.

Reads M5 candles from state/latest_candles.json, detects 20-bar channel
breakouts, and emits DecisionEngine-shaped candidates with `source` and
`risk_parity_*` annotations so the portfolio orchestrator can route them.

Stream name: "donchian_breakout"
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from strategies.donchian_breakout import DonchianParams, detect_signals
from core.utils import read_json_state, utc_now_iso


def donchian_enabled(config: dict[str, Any]) -> bool:
    return bool((config.get("strategies") or {}).get("diversification") or {}).get(
        "donchian_enabled", False
    )


def donchian_config(config: dict[str, Any]) -> DonchianParams:
    cfg = (config.get("strategies") or {}).get("diversification") or {}
    dn = cfg.get("donchian") or {}
    return DonchianParams(
        entry_len=int(dn.get("entry_len", 20)),
        exit_len=int(dn.get("exit_len", 10)),
        atr_len=int(dn.get("atr_len", 14)),
        atr_threshold=float(dn.get("atr_threshold", 0.0005)),
        use_double_band=bool(dn.get("use_double_band", True)),
        rr=float(dn.get("rr", 1.8)),
        sl_atr=float(dn.get("sl_atr", 1.5)),
    )


def _cooldown_hit(
    symbol: str,
    side: str,
    prev: dict[str, Any] | None,
    cooldown_seconds: int,
) -> bool:
    """Return True if (symbol, side) is still in cooldown. Timezone-safe.

    Accepts prev['timestamp'] as either ISO string or naive `utc_now_iso()`
    output. We ALWAYS parse as UTC (no implicit local-tz ambiguity) so the
    subtraction won't raise TypeError on mixed naive/aware datetimes.
    """
    if not prev or prev.get("side") != side:
        return False
    ts = prev.get("timestamp")
    if not ts:
        return False
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        if isinstance(ts, str):
            if ts.endswith("Z"):
                ts = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                ts = _dt.datetime.fromisoformat(ts).replace(tzinfo=_dt.timezone.utc)
        elif isinstance(ts, _dt.datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return (now - ts).total_seconds() < cooldown_seconds
    except (TypeError, ValueError) as exc:
        # Bad timestamp data — treat as no prior signal (conservative path).
        # Log explicitly so the operator sees bad data, NOT silent swallow.
        import logging as _lg
        _lg.getLogger("cooldown").warning(
            "cooldown_parse_failed symbol=%s side=%s ts=%r err=%s",
            symbol, side, ts, exc,
        )
        return False


def _stamp_cooldown(
    state: dict[str, dict[str, Any]] | None,
    symbol: str,
    side: str,
    now_iso: str,
) -> None:
    if state is None:
        return
    state[symbol] = {"side": side, "timestamp": now_iso}


def generate_donchian_signals(
    config: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
    _cooldown_state: dict[str, dict[str, Any]] | None = None,
    cooldown_seconds: int = 180,
) -> list[dict[str, Any]]:
    """Generate candidate signals from Donchian N-bar channel breakout."""
    log = logger or logging.getLogger("donchian_signal")
    if not donchian_enabled(config):
        return []

    params = donchian_config(config)
    div = (config.get("strategies") or {}).get("diversification") or {}
    symbols = list(div.get("donchian_symbols") or [])
    cooldown_seconds = int(div.get("cooldown_seconds", cooldown_seconds))

    candles_data = read_json_state("latest_candles.json", default={})
    symbol_candles = candles_data.get("symbols", {})
    if not symbols:
        symbols = list(symbol_candles.keys())

    out: list[dict[str, Any]] = []
    import uuid
    for symbol in symbols:
        sd = symbol_candles.get(symbol)
        if not sd:
            continue
        m5 = sd.get("M5", [])
        if len(m5) < params.entry_len + 30:
            log.debug("Donchian skip %s — insufficient candles (%d)", symbol, len(m5))
            continue
        try:
            import pandas as pd
            df = pd.DataFrame(m5).astype({"close": float, "open": float, "high": float, "low": float})
        except Exception as exc:
            log.warning("Donchian skip %s — DF build failed: %s", symbol, exc)
            continue
        try:
            long_bars, short_bars, bands = detect_signals(df, params)
        except Exception as exc:
            log.warning("Donchian skip %s — detect_signals failed: %s", symbol, exc)
            continue

        feat = read_json_state("features.json", default={}).get("symbols", {}).get(symbol, {})
        price = float(feat.get("price") or m5[-1].get("close") or 0.0)
        atr = float(feat.get("atr") or 0.0)
        if atr <= 0:
            try:
                from strategies.donchian_breakout import atr_series
                atr = float(atr_series(df, params.atr_len).iloc[-1])
            except Exception:
                atr = max(price * 0.001, 1e-5)

        all_bars = list(long_bars) + list(short_bars)
        if not all_bars:
            continue
        last_bar = max(int(x) for x in all_bars)
        is_long = last_bar in [int(x) for x in long_bars]
        is_short = last_bar in [int(x) for x in short_bars]
        if not (is_long or is_short):
            continue
        side = "BUY" if is_long else "SELL"

        # Determine band_label: the simulator places long/short bars in a
        # single array indexed by `bands`. We calibrated `bands[i]==1 -> fast`
        # (10-bar quick breakout). Find which band the last bar belonged to.
        band_label = "main"
        if len(long_bars) + len(short_bars) == len(bands) and len(bands) > 0:
            # Reconstruct side-sorted index: long bars come first then short.
            long_count = len(long_bars)
            if is_long:
                # The last long bar IS long_bars[-1]; find its position in concat.
                band_label = "fast" if bool(bands[long_count - 1]) else "main"
            else:
                band_label = "fast" if bool(bands[len(long_bars) + len(short_bars) - 1]) else "main"
        confidence = 78 if band_label == "main" else 65

        prev = (_cooldown_state or {}).get(symbol)
        if _cooldown_hit(symbol, side, prev, cooldown_seconds):
            continue

        sl_dist = params.sl_atr * atr
        reward_dist = sl_dist * params.rr
        if side == "BUY":
            sl = round(price - sl_dist, 5)
            tp1 = round(price + reward_dist, 5)
            tp2 = round(price + reward_dist * 1.6, 5)
        else:
            sl = round(price + sl_dist, 5)
            tp1 = round(price - reward_dist, 5)
            tp2 = round(price - reward_dist * 1.6, 5)

        candidate = {
            "signal_id": str(uuid.uuid4()),
            "symbol": symbol,
            "side": side,
            "setup_type": "donchian_breakout",
            "entry": round(price, 5),
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "entry_mode": "market",
            "order_type": "market",
            "within_reach": True,
            "confidence": confidence,
            "risk_parity_stream": "donchian_breakout",
            "market_context": {
                "regime": feat.get("volatility_regime", "unknown"),
                "m5_trend": feat.get("m5_trend", "neutral"),
                "session": "unknown",
                "market_regime": {"primary": "trending"},
            },
            "reason": f"Donchian {band_label}-band breakout ({params.entry_len if band_label == 'main' else params.exit_len}-bar)",
            "reasons": [f"Donchian {band_label}-band breakout",
                        f"SL {params.sl_atr}×ATR, TP {params.rr}R"],
            "source": "donchian_breakout",
            "donchian_params": params.as_dict(),
            "created_at": utc_now_iso(),
        }
        _stamp_cooldown(_cooldown_state, symbol, side, utc_now_iso())
        out.append(candidate)
        log.info(
            "Donchian %s %s %s band=%s conf=%d",
            symbol, side, "donchian_breakout", band_label, confidence,
        )
    return out
