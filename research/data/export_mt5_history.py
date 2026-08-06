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
    """
    Export available Parquet history to CSV files.
    
    Parameters:
    	symbols (list[str] | None): Symbols to export; defaults to the available symbols.
    	timeframes (list[str] | None): Timeframes to export; defaults to the configured timeframes.
    	output_dir (Path | None): Destination directory; defaults to the CSV export directory.
    
    Returns:
    	dict[str, Path]: A mapping from each exported symbol-timeframe key to its CSV path.
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
    """
    Copy available Parquet history files to a flat export directory.
    
    Parameters:
    	symbols (list[str] | None): Symbols to export. Defaults to all available symbols.
    	timeframes (list[str] | None): Timeframes to export. Defaults to the configured timeframes.
    	output_dir (Path | None): Destination directory. Defaults to the configured Parquet export directory.
    
    Returns:
    	dict[str, Path]: Mapping of symbol-timeframe keys to written Parquet file paths.
    """
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
    """
    Write a JSON manifest containing metadata and checksums for exported files.
    
    Parameters:
        written (dict[str, Path]): Mapping of file identifiers to exported file paths.
        path (Path | None): Destination path for the manifest. Defaults to the standard export directory.
    
    Returns:
        Path: Path to the written manifest.
    """
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
    """
    Run the complete export pipeline for the specified symbols and timeframes.
    
    Parameters:
    	symbols (list[str] | None): Symbols to export. Uses the configured research universe when omitted.
    	timeframes (list[str] | None): Timeframes to export. Uses the configured timeframes when omitted.
    
    Returns:
    	Path: Path to the generated export manifest.
    """
    csv = export_to_csv(symbols, timeframes)
    pq = export_to_parquet_flat(symbols, timeframes)
    return export_manifest({**csv, **pq})
