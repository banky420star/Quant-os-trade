"""Slippage model — estimated fill drift from historical trade records."""

from __future__ import annotations

from typing import Any

import numpy as np


def estimate_fill_drift(
    trades: list[dict[str, Any]],
    *,
    p95_only: bool = False,
) -> dict[str, float]:
    """
    Estimate absolute entry-price drift and optional ATR-relative drift from trade records.
    
    Parameters:
        trades (list[dict[str, Any]]): Trade records containing planned and actual entry
            prices, with an optional ATR value.
        p95_only (bool): Whether to include only 95th-percentile metrics.
    
    Returns:
        dict[str, float]: Mean and 95th-percentile drift metrics, including ATR-relative
        values when applicable. Returns an empty dictionary when no valid drift exists.
    """
    drifts: list[float] = []
    drifts_r: list[float] = []

    for t in trades:
        planned = t.get("planned_entry")
        actual = t.get("actual_fill")
        atr = t.get("atr")

        if planned is None or actual is None or planned == 0:
            continue

        drift = abs(float(actual) - float(planned))
        drifts.append(drift)

        if atr and float(atr) > 0:
            drifts_r.append(drift / float(atr))

    if not drifts:
        return {}

    result: dict[str, float] = {}
    if not p95_only:
        result["mean_drift"] = float(np.mean(drifts))
        result["mean_drift_R"] = float(np.mean(drifts_r)) if drifts_r else 0.0

    result["p95_drift"] = float(np.percentile(drifts, 95))
    if drifts_r:
        result["p95_drift_R"] = float(np.percentile(drifts_r, 95))

    return result
