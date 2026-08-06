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
    """
    Identify features that correlate with target values from future periods.
    
    A feature is marked suspicious when it has at least 30 shared non-null
    observations with the target and its absolute correlation at any tested lag
    exceeds the configured threshold.
    
    Parameters:
        features (pd.DataFrame): Feature values indexed by observation time.
        target (pd.Series): Target values indexed by observation time.
        max_lag (int): Maximum number of future periods to test.
        correlation_threshold (float): Absolute correlation above which a feature
            is considered suspicious.
    
    Returns:
        dict[str, Any]: Audit results containing the pass status, suspicious
            feature names, maximum absolute correlation and corresponding lag for
            each eligible feature, and the configured threshold.
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
    """
    Perform a lightweight structural audit for potential point-in-time data leakage.
    
    Parameters:
        df (pd.DataFrame): Data to inspect.
        timestamp_col (str): Name of the column containing timestamps.
    
    Returns:
        dict[str, Any]: Audit results containing ``passed`` and, when applicable,
        an ``issues`` list describing non-monotonic datetime indexes and
        future-looking column names. If no timestamp information is available,
        returns ``passed`` as ``True`` with a ``no_timestamp_column`` note.
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
