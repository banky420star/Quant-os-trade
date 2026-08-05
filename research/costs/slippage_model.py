"""Slippage model — estimated fill drift from historical trade records."""

from __future__ import annotations

from typing import Any

import numpy as np


def estimate_fill_drift(
    trades: list[dict[str, Any]],
    *,
    p95_only: bool = False,
) -> dict[str, float]:
    """Estimate fill drift from a list of closed-trade records.

    Each trade dict should contain:
        - ``planned_entry`` (float)
        - ``actual_fill`` (float or None)
        - ``symbol`` (str)
        - ``atr`` (float | None) — for R-relative drift

    Returns ``{mean_drift, p95_drift, mean_drift_R, p95_drift_R}``
    or a subset if *p95_only* is True.
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
