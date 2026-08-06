"""Point-in-time data catalog for the research plane.

Reads Parquet history written by ``loops/history_loop.py`` and builds a
time-indexed catalog with train/test splits that respect temporal ordering.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from core.utils import DATA_DIR, utc_now_iso

HISTORY_DIR = DATA_DIR / "history"
CATALOG_PATH = DATA_DIR / "catalog.json"

# ── symbol universe ──────────────────────────────────────────────────────────
RESEARCH_UNIVERSE: list[str] = [
    "XAUUSDm",
    "USOILm",
    "EURUSDm",
    "GBPUSDm",
    "USDJPYm",
    "USDCHFm",
    "AUDUSDm",
    "US500m",
    "US30m",
    "NAS100m",
    "UK100m",
    "FR40m",
    "JP225m",
    "BTCUSDm",
]

TIMEFRAMES: list[str] = ["D1", "H4", "H1", "M15", "M5"]


def parquet_path(symbol: str, timeframe: str) -> Path:
    """Return the canonical Parquet path for a symbol and timeframe."""
    return HISTORY_DIR / f"{symbol}_{timeframe}.parquet"


def read_parquet(symbol: str, timeframe: str) -> pd.DataFrame:
    """
    Load historical data for a symbol and timeframe.
    
    The ``time`` column is converted to UTC timestamps, set as the index, and
    the rows are ordered chronologically.
    
    Returns:
    	pd.DataFrame: The historical market data indexed by UTC timestamps.
    
    Raises:
    	FileNotFoundError: If the corresponding Parquet file does not exist.
    """
    path = parquet_path(symbol, timeframe)
    if not path.exists():
        raise FileNotFoundError(f"No history for {symbol} {timeframe}: {path}")
    df = pd.read_parquet(path)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time").sort_index()
    return df


def available_symbols() -> list[str]:
    """Return symbols that have at least one Parquet file on disk."""
    if not HISTORY_DIR.exists():
        return []
    stems = {p.stem for p in HISTORY_DIR.glob("*.parquet")}
    return sorted(
        sym
        for sym in RESEARCH_UNIVERSE
        if any(stem.startswith(f"{sym}_") for stem in stems)
    )


def available_timeframes(symbol: str) -> list[str]:
    """Return timeframes with Parquet data for *symbol*."""
    return sorted(
        p.stem.split("_", 1)[-1]
        for p in HISTORY_DIR.glob(f"{symbol}_*.parquet")
    )


def symbol_file_hash(symbol: str, timeframe: str) -> str:
    """
    Compute the SHA-256 hash of a symbol's Parquet history file.
    
    Returns:
        The hexadecimal SHA-256 digest, or an empty string if the file is missing.
    """
    path = parquet_path(symbol, timeframe)
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_catalog(
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    """
    Builds a point-in-time catalog of available market history and saves it to the catalog path.
    
    Parameters:
        symbols (list[str] | None): Symbols to include; defaults to available symbols.
        timeframes (list[str] | None): Timeframes to inspect; defaults to all supported timeframes.
    
    Returns:
        dict[str, Any]: Catalog containing metadata for each symbol, including timeframe coverage,
        row counts, file hashes, and the common date range.
    """
    symbols = symbols or available_symbols()
    timeframes = timeframes or TIMEFRAMES

    catalog: dict[str, Any] = {
        "created_at": utc_now_iso(),
        "research_universe": RESEARCH_UNIVERSE,
        "symbols": {},
    }

    for sym in symbols:
        entry: dict[str, Any] = {"timeframes": {}}
        common_start = None
        common_end = None

        for tf in timeframes:
            try:
                df = read_parquet(sym, tf)
            except FileNotFoundError:
                entry["timeframes"][tf] = None
                continue

            start_ts = df.index.min()
            end_ts = df.index.max()
            entry["timeframes"][tf] = {
                "rows": len(df),
                "start": start_ts.isoformat(),
                "end": end_ts.isoformat(),
                "hash": symbol_file_hash(sym, tf),
            }

            if common_start is None or start_ts > common_start:
                common_start = start_ts
            if common_end is None or end_ts < common_end:
                common_end = end_ts

        entry["earliest_common"] = common_start.isoformat() if common_start else None
        entry["latest_common"] = common_end.isoformat() if common_end else None
        catalog["symbols"][sym] = entry

    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps(catalog, indent=2, default=str), encoding="utf-8")
    return catalog


def train_test_split_date(
    symbols: list[str],
    timeframes: list[str],
    *,
    train_cutoff: str,
    test_start: str | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """
    Split historical data into training and test datasets using UTC date boundaries.
    
    Parameters:
    	symbols (list[str]): Symbols whose histories should be split.
    	timeframes (list[str]): Timeframes whose histories should be split.
    	train_cutoff (str): ISO date marking the end of the training period.
    	test_start (str | None): Optional ISO date marking the start of the test period.
    
    Returns:
    	tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]: Training and test datasets keyed by symbol and timeframe. Missing histories are omitted.
    """
    cutoff = pd.Timestamp(train_cutoff, tz="utc")
    gap_start = pd.Timestamp(test_start, tz="utc") if test_start else cutoff

    train: dict[str, pd.DataFrame] = {}
    test: dict[str, pd.DataFrame] = {}

    for sym in symbols:
        for tf in timeframes:
            try:
                df = read_parquet(sym, tf)
            except FileNotFoundError:
                continue
            key = f"{sym}_{tf}"
            train[key] = df[df.index < cutoff].copy()
            test[key] = df[df.index >= gap_start].copy()

    return train, test
