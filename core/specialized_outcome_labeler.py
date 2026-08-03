"""Specialized-setup outcome labeler (iteration 10, 2026-08-01).

Pure function that turns a single shadow fire into a realized R-multiple by
walking forward bars and checking intrabar SL/TP hits. This is the engine a
historical-replay shadow labeler will feed (next iteration): run the 14
specialized detectors over per-symbol M5 history -> shadow fires -> this
labeler -> per-cell win-rate/expectancy.

HONESTY choices (matter — these prevent overstating edge, per VERDICT.md and the
project's exit-model-bias findings):
  * Intrabar high/low for SL/TP hits, NOT close-to-close. The close-to-close exit
    model overstated edge by ~0.35R/cell (see memory exit-model-bias). Using
    intrabar highs/lows is the honest correction.
  * On same-bar SL+TP ambiguity, assume SL hits first (pessimistic). This is the
    conservative end of the intrabar ambiguity and biases AGAINST finding edge,
    which is the correct direction for a no-deployable-edge project.
  * Risk distance comes from the fire's own support/resistance (the detector's
    structural levels), so R is capital-independent and matches compute_stats.
  * If support/resistance is on the wrong side of entry (risk <= 0) the fire is
    invalid -> None (skipped, not counted as a win or loss). This filters
    garbage fires where the structural ref doesn't define real risk.
  * Time-stop exit at the last available bar (no look-ahead beyond max_bars).

This module places no orders, touches no kill switch, does no live trading.
"""

from __future__ import annotations

from typing import Any


def _risk_levels(fire: dict[str, Any], tp_r: float = 2.0) -> tuple[float, float, float, str] | None:
    """Resolve (entry, sl, tp, side) for a fire. Returns None if risk is invalid.

    BUY: sl = support (below entry), risk = entry - sl, tp = entry + tp_r * risk.
    SELL: sl = resistance (above entry), risk = sl - entry, tp = entry - tp_r*risk.
    """
    side = fire.get("side")
    entry = fire.get("entry")
    feat = fire.get("feat") or {}
    sl_ref = feat.get("support") if side == "BUY" else feat.get("resistance")
    if side not in ("BUY", "SELL") or entry is None or sl_ref is None:
        return None
    try:
        entry = float(entry)
        sl = float(sl_ref)
        tp_r = float(tp_r)
    except (TypeError, ValueError):
        return None
    if side == "BUY":
        risk = entry - sl
        if risk <= 0:
            return None  # support not below entry -> invalid risk
        tp = entry + tp_r * risk
    else:  # SELL
        risk = sl - entry
        if risk <= 0:
            return None  # resistance not above entry -> invalid risk
        tp = entry - tp_r * risk
    return (entry, sl, tp, side)


def label_outcome(
    fire: dict[str, Any],
    forward_bars: list[dict[str, Any]],
    *,
    max_bars: int = 48,
    tp_r: float | None = None,
) -> dict[str, Any] | None:
    """Compute the realized R-multiple for one fire over forward bars.

    ``fire`` = {side, entry, feat: {support|resistance}, tp_r?}.
    ``forward_bars`` = list of {time, high, low, close} bars AFTER the fire bar
    (index 0 = the bar immediately after entry).

    Returns {r_multiple, exit_reason, bars_held, entry, sl, tp, side} or None if
    the fire's risk is invalid (skipped, not counted).

    ``tp_r``: if given, overrides the fire's own ``tp_r`` (default = fire's tp_r
    or 2.0). Exit rules (intrabar, SL-first on same-bar ambiguity):
      * BUY  : bar.low <= sl -> -1.0 (SL); elif bar.high >= tp -> +tp_r (TP)
      * SELL : bar.high >= sl -> -1.0 (SL); elif bar.low <= tp -> +tp_r (TP)
      * Neither hit within max_bars -> time-stop at last close.
    """
    tp_r_eff = float(tp_r) if tp_r is not None else float(fire.get("tp_r", 2.0))
    levels = _risk_levels(fire, tp_r_eff)
    if levels is None:
        return None
    entry, sl, tp, side = levels
    bars = list(forward_bars)[:max_bars]
    if not bars:
        # no forward data -> cannot label, skip (not counted)
        return None

    for i, bar in enumerate(bars, start=1):
        try:
            high = float(bar.get("high"))
            low = float(bar.get("low"))
            close = float(bar.get("close"))
        except (TypeError, ValueError):
            continue
        if side == "BUY":
            # SL-first on same-bar ambiguity (pessimistic, biases against edge)
            if low <= sl:
                return {"r_multiple": -1.0, "exit_reason": "sl", "bars_held": i,
                        "entry": entry, "sl": sl, "tp": tp, "side": side}
            if high >= tp:
                return {"r_multiple": round(tp_r_eff, 4), "exit_reason": "tp",
                        "bars_held": i, "entry": entry, "sl": sl, "tp": tp,
                        "side": side}
        else:  # SELL
            if high >= sl:
                return {"r_multiple": -1.0, "exit_reason": "sl", "bars_held": i,
                        "entry": entry, "sl": sl, "tp": tp, "side": side}
            if low <= tp:
                return {"r_multiple": round(tp_r_eff, 4), "exit_reason": "tp",
                        "bars_held": i, "entry": entry, "sl": sl, "tp": tp,
                        "side": side}

    # Time-stop at last close.
    last_close = None
    for bar in reversed(bars):
        try:
            last_close = float(bar.get("close"))
            if last_close == last_close:  # not NaN
                break
        except (TypeError, ValueError):
            continue
    if last_close is None:
        return None
    if side == "BUY":
        risk = entry - sl
        r = (last_close - entry) / risk if risk > 0 else 0.0
    else:
        risk = sl - entry
        r = (entry - last_close) / risk if risk > 0 else 0.0
    return {"r_multiple": round(r, 4), "exit_reason": "time_stop",
            "bars_held": len(bars), "entry": entry, "sl": sl, "tp": tp,
            "side": side}


def label_fires(
    fires: list[dict[str, Any]],
    bars_by_symbol_time: dict[str, list[dict[str, Any]]],
    *,
    max_bars: int = 48,
    tp_r: float | None = None,
) -> list[dict[str, Any]]:
    """Label a batch of fires. ``bars_by_symbol_time`` maps symbol -> forward
    bars already aligned to start after each fire's bar (the replay caller is
    responsible for slicing). Returns labeled rows: the fire fields merged with
    {r_multiple, exit_reason, bars_held}. Skips invalid-risk / no-bar fires."""
    out: list[dict[str, Any]] = []
    for fire in fires:
        bars = bars_by_symbol_time.get(fire.get("symbol"), [])
        res = label_outcome(fire, bars, max_bars=max_bars, tp_r=tp_r)
        if res is None:
            continue
        row = dict(fire)
        row.update(res)
        out.append(row)
    return out