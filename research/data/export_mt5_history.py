"""Export MT5 history to portable formats for external research tools.

Writes Parquet and CSV copies of the history collected by
``loops/history_loop.py`` into ``data/exports/`` with reproducible
timestamps and optional symbol renaming.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from core.utils import DATA_DIR, utc_now_iso

from .catalog import RESEARCH_UNIVERSE, TIMEFRAMES, available_symbols, parquet_path

EXPORT_DIR = DATA_DIR / "exports"


def export_to_csv(
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    *,
    output_dir: Path | None = None,
) -> dict[str, Path]:
    """Export Parquet history to CSV files as ``{symbol}_{timeframe}.csv``.

    Returns a dict mapping key to output path.
    """
    symbols = symbols or available_symbols()
    timeframes = timeframes or TIMEFRAMES
    out = output_dir or (EXPORT_DIR / "csv")
    out.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for sym in symbols:
        for tf in timeframes:
            path = parquet_path(sym, tf)
            if not path.exists():
                continue
            df = pd.read_parquet(path)
            csv_path = out / f"{sym}_{tf}.csv"
            df.to_csv(csv_path, index=True)
            written[f"{sym}_{tf}"] = csv_path
    return written


def export_to_parquet_flat(
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    *,
    output_dir: Path | None = None,
) -> dict[str, Path]:
    """Flat-copy Parquet files (same schema, portable directory)."""
    symbols = symbols or available_symbols()
    timeframes = timeframes or TIMEFRAMES
    out = output_dir or (EXPORT_DIR / "parquet")
    out.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for sym in symbols:
        for tf in timeframes:
            src = parquet_path(sym, tf)
            if not src.exists():
                continue
            dst = out / f"{sym}_{tf}.parquet"
            dst.write_bytes(src.read_bytes())
            written[f"{sym}_{tf}"] = dst
    return written


def export_manifest(
    written: dict[str, Path],
    *,
    path: Path | None = None,
) -> Path:
    """Write a JSON manifest of exported files with hashes and timestamps."""
    import hashlib

    manifest_path = path or (EXPORT_DIR / "export_manifest.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    entries: dict[str, Any] = {}
    for key, p in sorted(written.items()):
        entries[key] = {
            "path": str(p.relative_to(EXPORT_DIR.parent)),
            "size_bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }

    doc: dict[str, Any] = {
        "exported_at": utc_now_iso(),
        "research_universe": RESEARCH_UNIVERSE,
        "timeframes": TIMEFRAMES,
        "files": entries,
    }
    manifest_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return manifest_path


def export_all(
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
) -> Path:
    """Run the full export pipeline: CSV + Parquet + manifest.  Returns manifest path."""
    csv = export_to_csv(symbols, timeframes)
    pq = export_to_parquet_flat(symbols, timeframes)
    return export_manifest({**csv, **pq})
