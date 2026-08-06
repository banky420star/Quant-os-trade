"""Data quality checks for imported history.

Detects:
  - missing bars (gaps larger than expected)
  - stale timestamps (bars not updating)
  - zero-volume or anomaly-volume bars
  - duplicate timestamps
  - out-of-order rows
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pandas as pd


@dataclass
class QualityReport:
    symbol: str
    timeframe: str
    total_rows: int = 0
    gaps: list[dict[str, Any]] = field(default_factory=list)
    stale_bars: int = 0
    duplicate_timestamps: int = 0
    zero_volume_bars: int = 0
    anomaly_volume_bars: int = 0
    out_of_order: int = 0
    passes: bool = True


def _expected_delta(timeframe: str) -> timedelta:
    """Return the expected interval between consecutive bars."""
    return {
        "D1": timedelta(days=1),
        "H4": timedelta(hours=4),
        "H1": timedelta(hours=1),
        "M15": timedelta(minutes=15),
        "M5": timedelta(minutes=5),
    }.get(timeframe, timedelta(days=1))


def _trading_day_gap_threshold(timeframe: str) -> timedelta | None:
    """Return the weekend and holiday allowance for supported timeframes."""
    return {
        "D1": timedelta(days=5),
        "H4": timedelta(hours=60),
        "H1": timedelta(hours=60),
    }.get(timeframe)


def run_quality_checks(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    *,
    volume_anomaly_multiple: float = 10.0,
    trading_day_aware: bool = True,
) -> QualityReport:
    """Run data-quality checks on a timestamp-indexed historical bar frame."""
    report = QualityReport(symbol=symbol, timeframe=timeframe)
    report.total_rows = len(df)

    if report.total_rows == 0:
        report.passes = False
        return report

    dupes = int(df.index.duplicated().sum())
    report.duplicate_timestamps = dupes
    if dupes > 0:
        report.passes = False

    if not df.index.is_monotonic_increasing:
        deltas = df.index.to_series().diff()
        report.out_of_order = int((deltas < pd.Timedelta(0)).sum())
        report.passes = False

    expected = _expected_delta(timeframe)
    diffs = df.index.to_series().diff()
    td_threshold = (
        _trading_day_gap_threshold(timeframe) if trading_day_aware else None
    )
    gap_mask = (
        diffs > td_threshold
        if td_threshold is not None
        else diffs > expected * 1.5
    )

    for ts, diff in diffs[gap_mask].items():
        report.gaps.append(
            {
                "after": ts.isoformat(),
                "gap": str(diff),
                "gap_bars": int(diff / expected),
            }
        )
        report.passes = False

    vol_col = next(
        (c for c in ("tick_volume", "real_volume", "volume") if c in df.columns),
        None,
    )
    if vol_col:
        report.zero_volume_bars = int((df[vol_col] == 0).sum())
        if report.zero_volume_bars > 0:
            report.passes = False

        median = df[vol_col].median()
        if median > 0:
            anomaly = df[vol_col] > median * volume_anomaly_multiple
            report.anomaly_volume_bars = int(anomaly.sum())

    if report.total_rows > 1:
        last = df.index[-1]
        allowance = td_threshold if td_threshold is not None else expected * 2
        recent = last + allowance
        if pd.Timestamp.now(tz=last.tz) > recent:
            report.stale_bars = 1
            report.passes = False

    return report


def quality_summary(reports: list[QualityReport]) -> str:
    """Create a human-readable summary of data-quality reports."""
    lines = ["Data Quality Summary", "=" * 40]
    failed = [r for r in reports if not r.passes]
    for report in reports:
        status = "PASS" if report.passes else "FAIL"
        detail = (
            f"gaps={len(report.gaps)} stale={report.stale_bars} "
            f"dupes={report.duplicate_timestamps} "
            f"zero_vol={report.zero_volume_bars} ooo={report.out_of_order}"
        )
        lines.append(
            f"  {report.symbol:12s} {report.timeframe:4s} {status:5s} "
            f"{report.total_rows:6d} rows  {detail}"
        )
    if failed:
        lines.append(
            f"\n{len(reports) - len(failed)}/{len(reports)} passed "
            f"({len(failed)} failed)"
        )
    else:
        lines.append(f"\n{len(reports)}/{len(reports)} passed")
    return "\n".join(lines)
