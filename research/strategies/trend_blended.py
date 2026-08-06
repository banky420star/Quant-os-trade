"""Blended daily trend strategy with volatility scaling and portfolio caps.

Implements the roadmap's Trend MVP:

  1. 1/3/6/12-month standardised momentum blend
  2. Volatility scaling (inverse vol position sizing)
  3. Correlation cluster caps
  4. Weekly rebalancing
  5. One position per symbol
  6. Exit on signal reversal
"""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pandas as pd

from .trend_baseline import blended_momentum_signal, signal_to_intent

# ── cluster definitions ──────────────────────────────────────────────────────
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
    """Return the cluster name for a symbol."""
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
) -> dict[str, dict[str, Any]]:
    """
    Generate daily trading intents from blended momentum signals with volatility-based sizing.
    
    Parameters:
        prices (dict[str, pd.Series]): Closing-price series keyed by symbol.
        horizons (tuple[int, ...]): Momentum lookback periods.
        threshold (float): Signal threshold used to classify momentum.
        vol_target (float): Target annualized volatility for position sizing.
        vol_lookback (int): Number of returns used to estimate volatility.
        cluster_caps (dict[str, float] | None): Optional exposure caps by symbol cluster.
    
    Returns:
        dict[str, dict[str, Any] | None]: Trading intents keyed by symbol; symbols with
        zero signals, insufficient data, invalid volatility, or no remaining cluster
        capacity map to None.
    """
    caps = cluster_caps or DEFAULT_CLUSTER_CAPS
    intents: dict[str, dict[str, Any] | None] = {}
    cluster_risk: dict[str, float] = {c: 0.0 for c in CLUSTERS}

    # 1. Compute raw signals and vols
    signals: dict[str, float] = {}
    vols: dict[str, float] = {}
    atrs: dict[str, float] = {}

    for sym, close in prices.items():
        if len(close) < max(horizons):
            continue

        raw = blended_momentum_signal(close, horizons=horizons, threshold=threshold)
        if raw.empty:
            continue

        signals[sym] = float(raw.iloc[-1])
        rets = close.pct_change().dropna()
        if len(rets) >= vol_lookback:
            vols[sym] = float(rets.tail(vol_lookback).std() * np.sqrt(252))
        else:
            vols[sym] = 0.0

        atr_est = close.tail(14).diff().abs().mean() if len(close) >= 14 else 0.0
        atrs[sym] = float(atr_est)

    # 2. Volatility-scaling weights
    vol_weights: dict[str, float] = {}
    for sym, sig in signals.items():
        if sig == 0 or vols.get(sym, 0) <= 0:
            vol_weights[sym] = 0.0
            continue
        # scale so each position contributes equal risk
        vol_weights[sym] = abs(sig) * (vol_target / vols[sym])

    # 3. Cluster caps
    for sym, weight in sorted(vol_weights.items(), key=lambda x: -x[1]):
        if weight <= 0:
            intents[sym] = None
            continue
        cluster = symbol_cluster(sym)
        cap = caps.get(cluster, 0.15)
        if cluster_risk[cluster] + weight > cap:
            weight = max(0.0, cap - cluster_risk[cluster])
        cluster_risk[cluster] += weight

        price = float(prices[sym].iloc[-1])
        atr = atrs.get(sym, 0.0)

        if weight > 0:
            intent = signal_to_intent(sym, signals[sym], price, atr=atr)
            if intent:
                intent["vol_weight"] = round(weight, 4)
            intents[sym] = intent
        else:
            intents[sym] = None

    return intents
