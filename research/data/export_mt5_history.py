"""Export MT5 history to portable CSV and Parquet files."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pandas as pd

from core.utils import DATA_DIR, utc_now_iso

from .catalog import RESEARCH_UNIVERSE, TIMEFRAMES, available_symbols, parquet_path

EXPORT_DIR = DATA_DIR / "exports"


def export_to_csv(symbols: list[str] | None = None, timeframes: list[str] | None = None, *, output_dir: Path | None = None) -> dict[str, Path]:
    """Export available history to CSV without a spurious RangeIndex column."""
    symbols = symbols or available_symbols()
    timeframes = timeframes or TIMEFRAMES
    out = output_dir or (EXPORT_DIR / "csv")
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for symbol in symbols:
        for timeframe in timeframes:
            source = parquet_path(symbol, timeframe)
            if not source.exists():
                continue
            frame = pd.read_parquet(source)
            destination = out / f"{symbol}_{timeframe}.csv"
            frame.to_csv(destination, index=not isinstance(frame.index, pd.RangeIndex))
            written[f"{symbol}_{timeframe}"] = destination
    return written


def export_to_parquet_flat(symbols: list[str] | None = None, timeframes: list[str] | None = None, *, output_dir: Path | None = None) -> dict[str, Path]:
    """Copy available Parquet history to a flat output directory."""
    symbols = symbols or available_symbols()
    timeframes = timeframes or TIMEFRAMES
    out = output_dir or (EXPORT_DIR / "parquet")
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for symbol in symbols:
        for timeframe in timeframes:
            source = parquet_path(symbol, timeframe)
            if not source.exists():
                continue
            destination = out / f"{symbol}_{timeframe}.parquet"
            shutil.copy2(source, destination)
            written[f"{symbol}_{timeframe}"] = destination
    return written


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_manifest(written: dict[str, Path], *, path: Path | None = None) -> Path:
    """Write metadata and checksums for every exported file."""
    manifest_path = path or (EXPORT_DIR / "export_manifest.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    entries: dict[str, Any] = {}
    for key, exported_path in sorted(written.items()):
        try:
            display_path = exported_path.relative_to(EXPORT_DIR.parent).as_posix()
        except ValueError:
            display_path = str(exported_path.resolve())
        entries[key] = {"path": display_path, "size_bytes": exported_path.stat().st_size, "sha256": _hash_file(exported_path)}
    document: dict[str, Any] = {"exported_at": utc_now_iso(), "research_universe": RESEARCH_UNIVERSE, "timeframes": TIMEFRAMES, "files": entries}
    manifest_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return manifest_path


def export_all(symbols: list[str] | None = None, timeframes: list[str] | None = None) -> Path:
    """Run CSV and Parquet exports and write one complete manifest."""
    csv_files = export_to_csv(symbols, timeframes)
    parquet_files = export_to_parquet_flat(symbols, timeframes)
    combined = {f"csv/{key}": value for key, value in csv_files.items()}
    combined.update({f"parquet/{key}": value for key, value in parquet_files.items()})
    return export_manifest(combined)
