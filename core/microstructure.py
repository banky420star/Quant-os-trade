"""Tick-level microstructure checks for fast scalping mode."""

from __future__ import annotations

from typing import Any


def spread_ok(
    spread_points: float,
    baseline_spread: float,
    *,
    max_mult: float = 1.2,
) -> tuple[bool, str]:
    if spread_points <= 0:
        return True, "spread_unknown"
    base = max(baseline_spread, spread_points * 0.5, 1.0)
    if spread_points > base * max_mult:
        return False, f"spread_spike {spread_points:.1f}>{base * max_mult:.1f}"
    return True, "spread_ok"


def anchor_distance_atr(mid: float, anchor: float, atr: float) -> float:
    if atr <= 0 or anchor <= 0:
        return 99.0
    return abs(mid - anchor) / atr


def price_in_zone(
    mid: float,
    anchor: float,
    atr: float,
    *,
    zone_atr: float,
) -> bool:
    if anchor <= 0 or atr <= 0:
        return False
    return abs(mid - anchor) <= atr * zone_atr


def tick_momentum_score(
    feat: dict[str, Any],
    side: str,
) -> tuple[float, str]:
    """Lightweight momentum from feature snapshot (no tick history required)."""
    rsi = float(feat.get("rsi_14") or feat.get("rsi") or 50)
    macd_hist = float(feat.get("macd_hist") or 0)
    vol_ratio = float(feat.get("volume_ratio") or feat.get("relative_volume") or 1.0)
    side_u = str(side).upper()

    score = 50.0
    if side_u == "BUY":
        if rsi > 52:
            score += 8
        if macd_hist > 0:
            score += 10
    elif side_u == "SELL":
        if rsi < 48:
            score += 8
        if macd_hist < 0:
            score += 10
    if vol_ratio >= 1.0:
        score += min(15, (vol_ratio - 1.0) * 20)
    elif vol_ratio < 0.7:
        score -= 10

    score = max(0.0, min(100.0, score))
    return score, "feat_momentum"


def entry_zone_bounds(anchor: float, atr: float, zone_atr: float) -> tuple[float, float]:
    half = atr * zone_atr
    return round(anchor - half, 5), round(anchor + half, 5)