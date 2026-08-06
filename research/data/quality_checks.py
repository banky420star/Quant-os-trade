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
    """Return the expected interval between consecutive bars for a timeframe.
    
    Parameters:
    	timeframe (str): Timeframe identifier such as ``D1``, ``H4``, ``H1``, ``M15``, or ``M5``.
    
    Returns:
    	timedelta: Expected interval between consecutive bars; one day for unsupported timeframes.
    """
    return {
        "D1": timedelta(days=1),
        "H4": timedelta(hours=4),
        "H1": timedelta(hours=1),
        "M15": timedelta(minutes=15),
        "M5": timedelta(minutes=5),
    }.get(timeframe, timedelta(days=1))


def _trading_day_gap_threshold(timeframe: str) -> timedelta:
    """
    Determine the maximum allowed gap for trading-day-aware timeframes.
    
    Parameters:
    	timeframe (str): Timeframe identifier used to select the gap threshold.
    
    Returns:
    	timedelta or None: Maximum acceptable gap for supported timeframes; None for unsupported timeframes.
    """
    return {
        "D1": timedelta(days=5),
        "H4": timedelta(hours=60),
        "H1": timedelta(hours=60),
    }.get(timeframe, None)


def run_quality_checks(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    *,
    volume_anomaly_multiple: float = 10.0,
    trading_day_aware: bool = True,
) -> QualityReport:
    """
    Run data-quality checks on a historical bar DataFrame.
    
    Parameters:
        df (pd.DataFrame): Historical bars indexed by timestamps.
        symbol (str): Symbol represented by the data.
        timeframe (str): Bar timeframe used to determine expected intervals.
        volume_anomaly_multiple (float): Multiple of the median volume used to identify anomalous bars.
        trading_day_aware (bool): Whether to allow weekend and holiday gaps for supported timeframes.
    
    Returns:
        QualityReport: Report containing quality findings and the overall pass status.
    """
    report = QualityReport(symbol=symbol, timeframe=timeframe)
    report.total_rows = len(df)

    if report.total_rows == 0:
        report.passes = False
        return report

    # -- duplicate timestamps -------------------------------------------------
    dupes = df.index.duplicated().sum()
    report.duplicate_timestamps = int(dupes)
    if dupes > 0:
        report.passes = False

    # -- out-of-order ---------------------------------------------------------
    if not df.index.is_monotonic_increasing:
        report.out_of_order = int((~df.index.is_monotonic_increasing).sum())
        report.passes = False

    # -- gaps -----------------------------------------------------------------
    expected = _expected_delta(timeframe)
    diffs = df.index.to_series().diff()

    # Trading-day-aware threshold: weekends + holidays for D1/H4/H1
    td_threshold = _trading_day_gap_threshold(timeframe) if trading_day_aware else None
    if td_threshold is not None:
        gap_mask = diffs > td_threshold
    else:
        gap_mask = diffs > expected * 1.5

    for ts, diff in diffs[gap_mask].items():
        report.gaps.append(
            {"after": ts.isoformat(), "gap": str(diff), "gap_bars": int(diff / expected)}
        )
        report.passes = False

    # -- zero / anomaly volume ------------------------------------------------
    vol_col = "tick_volume" if "tick_volume" in df.columns else ("real_volume" if "real_volume" in df.columns else None)
    if vol_col:
        report.zero_volume_bars = int((df[vol_col] == 0).sum())
        if report.zero_volume_bars > 0:
            report.passes = False

        median = df[vol_col].median()
        if median > 0:
            anomaly = df[vol_col] > median * volume_anomaly_multiple
            report.anomaly_volume_bars = int(anomaly.sum())

    # -- staleness ------------------------------------------------------------
    if report.total_rows > 1:
        last = df.index[-1]
        recent = last + expected * 2
        if pd.Timestamp.now(tz=last.tz) > recent:
            report.stale_bars = 1
            report.passes = False

    return report


def quality_summary(reports: list[QualityReport]) -> str:
    """
    Create a human-readable summary of data-quality reports.
    
    Parameters:
    	reports (list[QualityReport]): Quality reports to summarize.
    
    Returns:
    	str: Multiline summary containing each report's status and key metrics, followed by aggregate pass/fail totals.
    """
    lines = ["Data Quality Summary", "=" * 40]
    failed = [r for r in reports if not r.passes]
    for r in reports:
        status = "PASS" if r.passes else "FAIL"
        detail = (
            f"gaps={len(r.gaps)} stale={r.stale_bars} dupes={r.duplicate_timestamps}"
            f" zero_vol={r.zero_volume_bars} ooo={r.out_of_order}"
        )
        lines.append(f"  {r.symbol:12s} {r.timeframe:4s} {status:5s} {r.total_rows:6d} rows  {detail}")
    if failed:
        lines.append(f"\n{len(reports) - len(failed)}/{len(reports)} passed ({len(failed)} failed)")
    else:
        lines.append(f"\n{len(reports)}/{len(reports)} passed")
    return "\n".join(lines)
