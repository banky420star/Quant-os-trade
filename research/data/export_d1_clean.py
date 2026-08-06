#!/usr/bin/env python
"""Export clean timestamped D1 Parquet files to research/data/ with manifest.

Copies D1 Parquet from data/history/, standardises column ordering,
adds an ``exported_at`` metadata column, writes a comprehensive
``manifest.json``, then runs quality checks (gaps, dupes, stale bars,
volume anomalies) against every file.

Usage::

    PYTHONPATH=. python research/data/export_d1_clean.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

HISTORY_DIR = PROJECT_ROOT / "data" / "history"
OUT_DIR = Path(__file__).resolve().parent  # research/data/
MANIFEST_PATH = OUT_DIR / "manifest.json"

# -- symbol universe ----------------------------------------------------------
SYMBOLS = [
    "XAUUSDm", "USOILm", "EURUSDm", "GBPUSDm", "USDJPYm", "USDCHFm",
    "AUDUSDm", "US500m", "US30m", "NAS100m", "UK100m", "FR40m",
    "JP225m", "BTCUSDm",
]

EXPORTED_AT = datetime.now(timezone.utc).isoformat()


def _hash_file(path: Path) -> str:
    """
    Compute the SHA-256 hash of a file.
    
    Parameters:
        path (Path): Path to the file to hash.
    
    Returns:
        str: The hexadecimal SHA-256 digest, or an empty string if the file does not exist.
    """
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_one(symbol: str) -> dict[str, Any] | None:
    """
    Export a symbol's D1 Parquet history, enrich it with calendar and session metadata, and assess data quality.
    
    Parameters:
    	symbol (str): Symbol whose source history should be exported.
    
    Returns:
    	dict[str, Any] | None: Per-file export metadata and quality results, or an error record when the source file is missing.
    """
    src = HISTORY_DIR / f"{symbol}_D1.parquet"
    dst = OUT_DIR / f"{symbol}_D1.parquet"

    if not src.exists():
        return {"symbol": symbol, "error": f"source not found: {src}"}

    df = pd.read_parquet(src)

    # Standardise columns: time, open, high, low, close, volume
    col_map = {}
    for col in ["time", "open", "high", "low", "close"]:
        if col in df.columns:
            col_map[col] = col
    # Pick first volume column found
    for vcol in ["volume", "tick_volume", "real_volume"]:
        if vcol in df.columns:
            col_map[vcol] = "volume"
            break

    df = df.rename(columns=col_map)
    keep = [c for c in ["time", "open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep]

    # Ensure time is datetime, sorted
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.sort_values("time").reset_index(drop=True)

    # Add metadata column
    df["exported_at"] = EXPORTED_AT

    # -- derived columns -------------------------------------------------------
    dt = df["time"]

    # day_of_week: 0=Mon .. 6=Sun
    df["day_of_week"] = dt.dt.dayofweek

    # is_holiday: volume collapses + range tightens vs median
    has_vol = "volume" in df.columns
    if has_vol:
        med_vol = df["volume"].median()
        df["range"] = df["high"] - df["low"]
        med_range = df["range"].median()
        vol_collapse = df["volume"] < med_vol * 0.2
        range_tight = df["range"] < med_range * 0.3
        df["is_holiday"] = (vol_collapse & range_tight) if med_vol > 0 else False
        df.drop(columns=["range"], inplace=True)
    else:
        df["is_holiday"] = False

    # trading_session: label based on day_of_week + holiday flag
    def _session_label(row):
        """
        Classify a market data row by holiday and day-of-week session.
        
        Parameters:
            row: A row containing `day_of_week` and `is_holiday` values.
        
        Returns:
            The session label: `"thin"`, `"weekend"`, `"pre_weekend"`, or `"regular"`.
        """
        dow = row["day_of_week"]
        holiday = row["is_holiday"]
        if holiday:
            return "thin"
        if dow == 6:  # Sunday (BTC or data anomaly)
            return "weekend"
        if dow == 4:  # Friday
            return "pre_weekend"
        if dow == 5:  # Saturday
            return "weekend"
        return "regular"  # Mon-Thu

    df["trading_session"] = df.apply(_session_label, axis=1)

    df.to_parquet(dst, index=False)

    # Build metadata
    times = df["time"]
    info: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": "D1",
        "path": str(dst.relative_to(PROJECT_ROOT)),
        "rows": len(df),
        "columns": list(df.columns),
        "start": times.min().isoformat(),
        "end": times.max().isoformat(),
        "exported_at": EXPORTED_AT,
        "sha256": _hash_file(dst),
        "size_bytes": dst.stat().st_size,
    }

    # Gaps check — D1 bars skip weekends and holidays.
    # Normal gaps: 1 day (overnight), 3 days (Fri→Mon), up to 5 days
    # for holiday weekends.  Flag gaps > 5 calendar days as suspicious.
    diffs = times.diff().dropna()
    # Count trading days vs calendar days
    calendar_days = (times.iloc[-1] - times.iloc[0]).days
    expected_bars = calendar_days * 5 / 7  # ~5 trading days per week
    info["bars_vs_expected"] = round(len(times) / max(expected_bars, 1), 3)
    # Flag gaps > 5 calendar days (longer than a holiday weekend)
    gap_mask = diffs > pd.Timedelta(days=5)
    info["gaps"] = []
    for i, diff in diffs[gap_mask].items():
        info["gaps"].append({
            "after": times.iloc[i].isoformat(),
            "gap_days": str(diff),
        })
    info["weekend_gaps_skipped"] = int((diffs > pd.Timedelta(days=1)).sum()) - len(info["gaps"])

    # Duplicate check
    dupes = int(times.duplicated().sum())
    info["duplicate_timestamps"] = dupes

    # Staleness check
    last = times.iloc[-1]
    expected_next = last + pd.Timedelta(days=2)
    info["stale"] = pd.Timestamp.now(tz="utc") > expected_next

    # Volume check
    if "volume" in df.columns:
        info["zero_volume_bars"] = int((df["volume"] == 0).sum())
        median = df["volume"].median()
        if median > 0:
            anomaly = df["volume"] > median * 10
            info["anomaly_volume_bars"] = int(anomaly.sum())
        else:
            info["anomaly_volume_bars"] = 0
    else:
        info["zero_volume_bars"] = 0
        info["anomaly_volume_bars"] = 0

    info["quality_pass"] = (
        len(info["gaps"]) == 0  # only real gaps (not weekends)
        and info["duplicate_timestamps"] == 0
        and not info["stale"]
    )

    # Calendar stats from derived columns
    info["holiday_bars"] = int(df["is_holiday"].sum())
    info["session_counts"] = df["trading_session"].value_counts().to_dict()
    for k in ["regular", "pre_weekend", "weekend", "thin"]:
        info["session_counts"].setdefault(k, 0)

    return info


def main() -> int:
    """
    Export D1 Parquet history for all configured symbols and write the export manifest.
    
    Returns:
    	int: `0` if every exported file passes quality checks, `1` otherwise.
    """
    print(f"Exporting D1 Parquet to {OUT_DIR}")
    print(f"  Symbols: {len(SYMBOLS)}")
    print()

    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    header = f"{'Symbol':12s} {'Rows':>6s} {'Start':>12s} {'End':>12s} {'Gaps>5d':>7s} {'Dupes':>5s} {'Bars%':>6s} {'Quality':>7s}"
    print(header)
    print("-" * 75)

    for sym in SYMBOLS:
        info = export_one(sym)
        if info is None:
            continue
        if "error" in info:
            errors.append(info)
            print(f"  {sym:10s}  ERROR: {info['error'][:40]}")
            continue
        entries.append(info)
        q = "PASS" if info["quality_pass"] else "FAIL"
        start_s = info["start"][:10]
        end_s = info["end"][:10]
        bars_pct = info.get("bars_vs_expected", 0)
        print(
            f"  {sym:10s} {info['rows']:>6d} {start_s:>12s} {end_s:>12s} "
            f"{len(info['gaps']):>7d} {info['duplicate_timestamps']:>5d} {bars_pct:>6.3f} {q:>7s}"
        )

    # -- summary --------------------------------------------------------------
    print()
    total_rows = sum(e["rows"] for e in entries)
    total_gaps = sum(len(e["gaps"]) for e in entries)
    total_dupes = sum(e["duplicate_timestamps"] for e in entries)
    quality_pass = sum(1 for e in entries if e["quality_pass"])
    stale_count = sum(1 for e in entries if e.get("stale"))
    total_holidays = sum(e.get("holiday_bars", 0) for e in entries)
    total_regular = sum(e.get("session_counts", {}).get("regular", 0) for e in entries)
    total_pre_wknd = sum(e.get("session_counts", {}).get("pre_weekend", 0) for e in entries)
    total_weekend = sum(e.get("session_counts", {}).get("weekend", 0) for e in entries)
    total_thin = sum(e.get("session_counts", {}).get("thin", 0) for e in entries)
    print(f"  Total: {len(entries)} files, {total_rows:,} rows")
    print(f"  Gaps: {total_gaps}  Dupes: {total_dupes}  Stale: {stale_count}  Holidays: {total_holidays}")
    print(f"  Sessions: regular={total_regular} pre_weekend={total_pre_wknd} weekend={total_weekend} thin={total_thin}")
    print(f"  Quality: {quality_pass}/{len(entries)} pass")

    # -- cross-symbol alignment ------------------------------------------------
    starts = [e["start"] for e in entries]
    ends = [e["end"] for e in entries]
    alignment = {
        "earliest_start": min(starts),
        "latest_start": max(starts),
        "earliest_end": min(ends),
        "latest_end": max(ends),
        "starts_aligned": len(set(starts)) <= 1,
        "ends_aligned": len(set(ends)) <= 1,
    }

    # -- manifest --------------------------------------------------------------
    manifest = {
        "manifest_version": "1.0.0",
        "generated_at": EXPORTED_AT,
        "source": "MT5 D1 history via loops/history_loop.py",
        "source_dir": str(HISTORY_DIR),
        "output_dir": str(OUT_DIR.relative_to(PROJECT_ROOT)),
        "total_files": len(entries),
        "total_rows": total_rows,
        "symbols": SYMBOLS,
        "timeframe": "D1",
        "quality": {
            "pass": quality_pass,
            "total": len(entries),
            "total_gaps": total_gaps,
            "total_dupes": total_dupes,
            "stale_files": stale_count,
        },
        "alignment": alignment,
        "files": {e["symbol"]: e for e in sorted(entries, key=lambda x: x["symbol"])},
        "errors": errors if errors else None,
    }

    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    print(f"\nManifest written to {MANIFEST_PATH}")

    return 0 if quality_pass == len(entries) else 1


if __name__ == "__main__":
    sys.exit(main())
