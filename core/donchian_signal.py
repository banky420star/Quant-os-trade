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
    return bool(((config.get("strategies") or {}).get("diversification") or {}).get(
        "donchian_enabled", False
    ))


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
                atr = price * 0.001
        if price <= 0 or atr <= 0:
            continue

        # Check cooldown
        prev = _cooldown_state.get(symbol) if _cooldown_state else None
        now_iso = utc_now_iso()

        for side, bars_list in [("BUY", long_bars), ("SELL", short_bars)]:
            if len(bars_list) == 0:
                continue
            if _cooldown_hit(symbol, side, prev, cooldown_seconds):
                log.debug("Donchian cooldown active %s %s", symbol, side)
                continue

            last_bar_idx = bars_list[-1]
            sl_dist = params.sl_atr * atr
            if side == "BUY":
                entry = float(df.iloc[last_bar_idx]["close"])
                sl = entry - sl_dist
                tp = entry + params.rr * sl_dist
            else:
                entry = float(df.iloc[last_bar_idx]["close"])
                sl = entry + sl_dist
                tp = entry - params.rr * sl_dist

            _stamp_cooldown(_cooldown_state, symbol, side, now_iso)

            out.append({
                "symbol": symbol,
                "side": side,
                "confidence": 75,
                "score": 0.0,
                "source": "donchian_breakout",
                "setup_type": "donchian_breakout",
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "risk_parity_share": div.get("risk_shares", {}).get("donchian_breakout", 0.25),
                "timestamp": now_iso,
                "id": str(uuid.uuid4())[:8],
            })
            log.info(
                "Donchian %s %s entry=%.5f sl=%.5f tp=%.5f",
                symbol, side, entry, sl, tp,
            )

    return out
