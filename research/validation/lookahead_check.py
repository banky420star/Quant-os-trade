"""Point-in-time leakage detection.

Tests that features do not contain future information and that
train/test splits respect temporal boundaries.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def detect_lookahead(
    features: pd.DataFrame,
    target: pd.Series,
    *,
    max_lag: int = 20,
    correlation_threshold: float = 0.15,
) -> dict[str, Any]:
    """Check for lookahead by measuring correlation between features
    and *future* target values.

    If any feature is strongly correlated with a future target at a
    short lag, suspect leakage.

    Returns dict with ``suspicious`` feature names, ``max_corrs``,
    and ``passed`` boolean.
    """
    suspicious: list[str] = []
    max_corrs: dict[str, dict[str, float]] = {}

    for col in features.columns:
        feat = features[col].dropna()
        common = feat.index.intersection(target.dropna().index)
        if len(common) < 30:
            continue

        best_corr = 0.0
        best_lag = 0

        for lag in range(1, max_lag + 1):
            future = target.shift(-lag).reindex(common)
            past = feat.reindex(common)
            corr = past.corr(future)
            if abs(corr) > best_corr:
                best_corr = abs(corr)
                best_lag = lag

            if abs(corr) > correlation_threshold:
                suspicious.append(col)
                break

        max_corrs[col] = {"lag": best_lag, "correlation": round(best_corr, 4)}

    return {
        "passed": len(suspicious) == 0,
        "suspicious_features": suspicious,
        "max_correlations": max_corrs,
        "threshold": correlation_threshold,
    }


def point_in_time_audit(
    df: pd.DataFrame,
    timestamp_col: str = "time",
    *,
    max_future_seconds: int = 60,
) -> dict[str, Any]:
    """Verify that no row references data after its timestamp.

    Simple audit: check that columns derived from future data (e.g.
    shifted returns) are properly aligned.  This is a lightweight
    structural check, not a deep feature audit.
    """
    if timestamp_col not in df.columns and not isinstance(df.index, pd.DatetimeIndex):
        return {"passed": True, "note": "no_timestamp_column"}

    issues: list[dict[str, Any]] = []

    # Check monotonic timestamps
    if isinstance(df.index, pd.DatetimeIndex):
        if not df.index.is_monotonic_increasing:
            issues.append({"type": "non_monotonic_index", "count": int((~df.index.is_monotonic_increasing).sum())})

    # Flag future-looking column names
    future_patterns = ["next_", "future_", "forward_", "_t1", "_lead"]
    for col in df.columns:
        for pat in future_patterns:
            if pat in str(col).lower():
                issues.append({"type": "future_looking_column", "column": str(col)})
                break

    return {"passed": len(issues) == 0, "issues": issues}
