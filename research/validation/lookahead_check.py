"""Point-in-time leakage detection."""

from __future__ import annotations

from typing import Any

import pandas as pd


def detect_lookahead(
    features: pd.DataFrame,
    target: pd.Series,
    *,
    max_lag: int = 20,
    correlation_threshold: float = 0.15,
) -> dict[str, Any]:
    """Identify features that correlate with future target values."""
    suspicious: list[str] = []
    max_corrs: dict[str, dict[str, float]] = {}
    target_non_null = target.dropna()

    for col in features.columns:
        feat = features[col].dropna()
        common = feat.index.intersection(target_non_null.index)
        if len(common) < 30:
            continue

        best_corr = 0.0
        best_lag = 0
        for lag in range(1, max_lag + 1):
            future = target.shift(-lag).reindex(common)
            past = feat.reindex(common)
            corr = past.corr(future)
            if pd.isna(corr):
                continue
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
    """Audit ordering, future timestamps, and future-looking column names."""
    has_datetime_index = isinstance(df.index, pd.DatetimeIndex)
    if timestamp_col not in df.columns and not has_datetime_index:
        return {"passed": True, "note": "no_timestamp_column"}

    issues: list[dict[str, Any]] = []

    if has_datetime_index and not df.index.is_monotonic_increasing:
        regressions = int(
            (df.index.to_series().diff() < pd.Timedelta(0)).sum()
        )
        issues.append({"type": "non_monotonic_index", "count": regressions})

    if timestamp_col in df.columns:
        timestamps = pd.to_datetime(df[timestamp_col], utc=True, errors="coerce")
        future_limit = pd.Timestamp.now(tz="utc") + pd.Timedelta(
            seconds=max_future_seconds
        )
        future_count = int((timestamps > future_limit).sum())
        if future_count:
            issues.append({"type": "future_timestamp", "count": future_count})

    future_patterns = ["next_", "future_", "forward_", "_t1", "_lead"]
    for col in df.columns:
        if any(pattern in str(col).lower() for pattern in future_patterns):
            issues.append({"type": "future_looking_column", "column": str(col)})

    return {"passed": len(issues) == 0, "issues": issues}
