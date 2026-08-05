#!/usr/bin/env python
"""Export all MT5 history Parquet files to ``research/data/exports/``.

Dumps every symbol x timeframe pair as both Parquet (flat copy) and CSV,
then writes ``manifest.json`` with per-file hashes, row counts, and date
ranges so external tools (VectorBT, Qlib, Freqtrade, Jupyter notebooks)
can discover and load the data reliably.

Usage::

    python research/data/export_all.py                  # all symbols, all TFs
    python research/data/export_all.py --symbols XAUUSDm EURUSDm
    python research/data/export_all.py --timeframes D1 H4
    python research/data/export_all.py --csv-only
    python research/data/export_all.py --parquet-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

# Project-root-relative imports
_THIS_DIR = Path(__file__).resolve().parent
_RESEARCH_DIR = _THIS_DIR
_PROJECT_ROOT = _RESEARCH_DIR.parents[1]

# Add project root so we can import core.utils
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.utils import utc_now_iso  # noqa: E402

#  paths 
HISTORY_DIR = _PROJECT_ROOT / "data" / "history"
EXPORTS_DIR = _RESEARCH_DIR / "exports"
PARQUET_OUT = EXPORTS_DIR / "parquet"
CSV_OUT = EXPORTS_DIR / "csv"

#  universe 
ALL_SYMBOLS = sorted(
    list({p.stem.split("_")[0] for p in HISTORY_DIR.glob("*.parquet")})
)
ALL_TIMEFRAMES = sorted(
    list({p.stem.rsplit("_", 1)[-1] for p in HISTORY_DIR.glob("*.parquet")})
)

#  helpers 


def _hash_file(path: Path) -> str:
    """SHA-256 of a file, or empty string if missing."""
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _preview_parquet(path: Path) -> dict[str, Any] | None:
    """Extract per-file metadata: rows, start, end, columns, dtypes."""
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        return None
    meta: dict[str, Any] = {
        "rows": len(df),
        "columns": list(df.columns),
    }
    if "time" in df.columns:
        times = pd.to_datetime(df["time"], utc=True)
        meta["start"] = times.min().isoformat()
        meta["end"] = times.max().isoformat()
        meta["bars_per_day"] = round(len(df) / max(1, (times.max() - times.min()).days), 2)
    else:
        meta["start"] = None
        meta["end"] = None
    return meta


def _all_same(items: list[Any]) -> bool:
    return len(set(items)) <= 1


#  export functions 


def export_parquet(
    symbols: list[str],
    timeframes: list[str],
) -> dict[str, Path]:
    """Flat-copy Parquet files to the export directory."""
    PARQUET_OUT.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    for sym in symbols:
        for tf in timeframes:
            src = HISTORY_DIR / f"{sym}_{tf}.parquet"
            if not src.exists():
                continue
            dst = PARQUET_OUT / f"{sym}_{tf}.parquet"
            shutil.copy2(src, dst)
            written[f"{sym}_{tf}"] = dst

    return written


def export_csv(
    symbols: list[str],
    timeframes: list[str],
) -> dict[str, Path]:
    """Export Parquet to CSV (includes time index)."""
    CSV_OUT.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    for sym in symbols:
        for tf in timeframes:
            src = HISTORY_DIR / f"{sym}_{tf}.parquet"
            if not src.exists():
                continue
            df = pd.read_parquet(src)
            dst = CSV_OUT / f"{sym}_{tf}.csv"
            df.to_csv(dst, index=True)
            written[f"{sym}_{tf}"] = dst

    return written


def build_manifest(
    written_parquet: dict[str, Path],
    written_csv: dict[str, Path],
    symbols: list[str],
    timeframes: list[str],
) -> dict[str, Any]:
    """Build a comprehensive manifest with per-file metadata and summaries."""
    files: dict[str, Any] = {}
    per_symbol: dict[str, Any] = {}
    per_timeframe: dict[str, Any] = {}

    for sym in symbols:
        per_symbol[sym] = {"timeframes": {}}
        for tf in timeframes:
            pq_key = f"{sym}_{tf}"
            pq_path = written_parquet.get(pq_key)
            csv_path = written_csv.get(pq_key)

            meta = _preview_parquet(HISTORY_DIR / f"{sym}_{tf}.parquet")
            if meta is None:
                per_symbol[sym]["timeframes"][tf] = None
                continue

            pq_hash = _hash_file(pq_path) if pq_path else ""
            csv_hash = _hash_file(csv_path) if csv_path else ""

            entry = {
                "symbol": sym,
                "timeframe": tf,
                "rows": meta["rows"],
                "start": meta["start"],
                "end": meta["end"],
                "bars_per_day": meta.get("bars_per_day"),
                "columns": meta.get("columns", []),
                "parquet_path": str(pq_path.relative_to(EXPORTS_DIR.parent)) if pq_path else None,
                "parquet_bytes": pq_path.stat().st_size if pq_path else 0,
                "parquet_sha256": pq_hash,
                "csv_path": str(csv_path.relative_to(EXPORTS_DIR.parent)) if csv_path else None,
                "csv_bytes": csv_path.stat().st_size if csv_path else 0,
                "csv_sha256": csv_hash,
            }
            files[pq_key] = entry
            per_symbol[sym]["timeframes"][tf] = {
                "rows": meta["rows"],
                "start": meta["start"],
                "end": meta["end"],
            }

            per_timeframe.setdefault(tf, {"total_rows": 0, "symbols": 0})
            per_timeframe[tf]["total_rows"] += meta["rows"]
            per_timeframe[tf]["symbols"] += 1

    # Symbol-level summaries
    for sym, sym_data in per_symbol.items():
        rows = [t["rows"] for t in sym_data["timeframes"].values() if t]
        starts = [t["start"] for t in sym_data["timeframes"].values() if t]
        ends = [t["end"] for t in sym_data["timeframes"].values() if t]
        sym_data["total_files"] = len(rows)
        sym_data["total_rows"] = sum(rows)
        sym_data["earliest"] = min(starts) if starts else None
        sym_data["latest"] = min(ends) if ends else None  # conservative common end

    # Cross-symbol consistency check
    consistency: dict[str, Any] = {}
    for tf in timeframes:
        starts = []
        ends = []
        for sym in symbols:
            s_data = per_symbol.get(sym, {}).get("timeframes", {}).get(tf)
            if s_data:
                starts.append(s_data["start"])
                ends.append(s_data["end"])
        consistency[tf] = {
            "symbols": len(starts),
            "starts_aligned": _all_same(starts) if starts else False,
            "ends_aligned": _all_same(ends) if ends else False,
            "earliest_start": min(starts) if starts else None,
            "latest_end": max(ends) if ends else None,
        }

    doc: dict[str, Any] = {
        "manifest_version": "1.0.0",
        "exported_at": utc_now_iso(),
        "source": "MT5 history via loops/history_loop.py",
        "source_dir": str(HISTORY_DIR),
        "total_symbols": len(symbols),
        "total_timeframes": len(timeframes),
        "total_files": len(files),
        "total_rows": sum(f["rows"] for f in files.values()),
        "symbols": symbols,
        "timeframes": timeframes,
        "export_dirs": {
            "parquet": str(PARQUET_OUT.relative_to(EXPORTS_DIR.parent)),
            "csv": str(CSV_OUT.relative_to(EXPORTS_DIR.parent)),
        },
        "consistency": consistency,
        "per_timeframe_summary": per_timeframe,
        "per_symbol_summary": per_symbol,
        "files": files,
    }
    return doc


def export_all(
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    *,
    parquet: bool = True,
    csv: bool = True,
) -> Path:
    """Run the full export pipeline.  Returns path to ``manifest.json``."""
    symbols = symbols or ALL_SYMBOLS
    timeframes = timeframes or ALL_TIMEFRAMES

    written_pq: dict[str, Path] = {}
    written_csv: dict[str, Path] = {}

    print(f"Exporting {len(symbols)} symbols x {len(timeframes)} timeframes -> {EXPORTS_DIR}")
    print(f"  Symbols:   {symbols}")
    print(f"  Timeframes: {timeframes}")

    if parquet:
        written_pq = export_parquet(symbols, timeframes)
        print(f"  Parquet:   {len(written_pq)} files -> {PARQUET_OUT}")

    if csv:
        written_csv = export_csv(symbols, timeframes)
        print(f"  CSV:       {len(written_csv)} files -> {CSV_OUT}")

    manifest = build_manifest(written_pq, written_csv, symbols, timeframes)
    manifest_path = EXPORTS_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    print(f"  Manifest:  {manifest_path}")

    # Quick summary
    total_rows = manifest["total_rows"]
    print(f"\nTotal: {len(manifest['files'])} files, {total_rows:,} rows across {len(symbols)} symbols")

    for sym, sym_data in manifest["per_symbol_summary"].items():
        rows = sym_data["total_rows"]
        earliest = sym_data.get("earliest", "?")[:10] if sym_data.get("earliest") else "?"
        latest = sym_data.get("latest", "?")[:10] if sym_data.get("latest") else "?"
        print(f"  {sym:12s}  {rows:>8,} rows  {earliest} -> {latest}")

    return manifest_path


#  CLI 


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export MT5 history to research/data/exports/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--symbols", nargs="*",
        help="Symbols to export (default: all available)",
    )
    parser.add_argument(
        "--timeframes", nargs="*",
        help="Timeframes to export (default: M5 M15 H4 D1)",
    )
    parser.add_argument(
        "--csv-only", action="store_true",
        help="Export CSV only (skip Parquet)",
    )
    parser.add_argument(
        "--parquet-only", action="store_true",
        help="Export Parquet only (skip CSV)",
    )
    args = parser.parse_args()

    symbols = args.symbols or ALL_SYMBOLS
    timeframes = args.timeframes or ALL_TIMEFRAMES

    pq = not args.csv_only
    csv = not args.parquet_only

    manifest_path = export_all(symbols, timeframes, parquet=pq, csv=csv)
    print(f"\nDone.  Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
