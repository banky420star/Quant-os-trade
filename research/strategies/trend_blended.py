"""Blended daily trend strategy with volatility scaling and cluster caps."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd

from .trend_baseline import blended_momentum_signal, signal_to_intent

CLUSTERS: dict[str, list[str]] = {
    "equity_indices": ["US500m", "US30m", "NAS100m", "UK100m", "FR40m", "JP225m"],
    "fx_usd_majors": ["EURUSDm", "GBPUSDm", "AUDUSDm", "USDCHFm", "USDJPYm"],
    "metals": ["XAUUSDm"],
    "energy": ["USOILm"],
    "crypto": ["BTCUSDm"],
}

DEFAULT_CLUSTER_CAPS: dict[str, float] = {
    "equity_indices": 0.35,
    "fx_usd_majors": 0.35,
    "metals": 0.15,
    "energy": 0.15,
    "crypto": 0.10,
}


def symbol_cluster(symbol: str) -> str:
    for name, members in CLUSTERS.items():
        if symbol in members:
            return name
    return "other"


def blended_daily_signal(
    prices: dict[str, pd.Series],
    *,
    horizons: tuple[int, ...] = (21, 63, 126, 252),
    threshold: float = 0.3,
    vol_target: float = 0.15,
    vol_lookback: int = 63,
    cluster_caps: dict[str, float] | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Generate nullable trading intents for every input symbol."""
    caps = cluster_caps or DEFAULT_CLUSTER_CAPS
    intents: dict[str, dict[str, Any] | None] = {}
    cluster_risk: defaultdict[str, float] = defaultdict(float)
    signals: dict[str, float] = {}
    vols: dict[str, float] = {}
    atrs: dict[str, float] = {}

    for symbol, close in prices.items():
        intents[symbol] = None
        if len(close) < max(horizons):
            continue
        raw = blended_momentum_signal(close, horizons=horizons, threshold=threshold)
        if raw.empty:
            continue
        signals[symbol] = float(raw.iloc[-1])
        returns = close.pct_change().dropna()
        vols[symbol] = float(returns.tail(vol_lookback).std() * np.sqrt(252)) if len(returns) >= vol_lookback else 0.0
        atrs[symbol] = float(close.tail(14).diff().abs().mean() if len(close) >= 14 else 0.0)

    vol_weights: dict[str, float] = {}
    for symbol, signal in signals.items():
        volatility = vols.get(symbol, 0.0)
        vol_weights[symbol] = abs(signal) * (vol_target / volatility) if signal != 0 and volatility > 0 else 0.0

    for symbol, raw_weight in sorted(vol_weights.items(), key=lambda item: -item[1]):
        if raw_weight <= 0:
            continue
        cluster = symbol_cluster(symbol)
        cap = caps.get(cluster, 0.15)
        weight = min(raw_weight, max(0.0, cap - cluster_risk[cluster]))
        cluster_risk[cluster] += weight
        if weight <= 0:
            continue
        intent = signal_to_intent(symbol, signals[symbol], float(prices[symbol].iloc[-1]), atr=atrs.get(symbol, 0.0))
        if intent:
            intent["vol_weight"] = round(weight, 4)
        intents[symbol] = intent
    return intents
