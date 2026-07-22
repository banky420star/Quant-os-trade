"""History Manager — download, store Parquet, incremental updates, serve loops."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from core.data_collector import DataCollector
from core.utils import DATA_DIR, utc_now_iso

HISTORY_DIR = DATA_DIR / "history"
_PARQUET_LOCKS: dict[str, threading.Lock] = {}
_PARQUET_LOCKS_GUARD = threading.Lock()
_MIN_PARQUET_BYTES = 12
_REPLACE_RETRIES = 10
_READ_RETRIES = 6


def _parquet_thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _PARQUET_LOCKS_GUARD:
        if key not in _PARQUET_LOCKS:
            _PARQUET_LOCKS[key] = threading.Lock()
        return _PARQUET_LOCKS[key]


@contextlib.contextmanager
def _parquet_file_lock(path: Path) -> Iterator[None]:
    """Cross-process exclusive lock for parquet writes (Windows-safe)."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if sys.platform == "win32":
            try:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _atomic_replace(src: Path, dst: Path) -> None:
    """Replace dst with src, retrying on Windows file-lock races."""
    last_err: OSError | None = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last_err = exc
            if attempt < _REPLACE_RETRIES - 1:
                time.sleep(0.04 * (2 ** attempt))
    if last_err:
        raise last_err


class HistoryManager:
    """Manage long-term candle storage in Parquet format."""

    def __init__(
        self,
        config: dict[str, Any],
        collector: DataCollector | None = None,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self.collector = collector
        self.logger = logger or logging.getLogger("history_manager")
        self.history_cfg = config.get("history", {})
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    def parquet_path(self, logical_symbol: str, timeframe: str) -> Path:
        return HISTORY_DIR / f"{logical_symbol}_{timeframe}.parquet"

    def download_full(self, symbol_map: dict[str, str], timeframe: str = "M5") -> dict[str, Any]:
        """Download up to max_bars candles per symbol."""
        max_bars = int(self.history_cfg.get("max_bars", 50000))
        chunk = int(self.history_cfg.get("chunk_size", 5000))
        results: dict[str, Any] = {"timestamp": utc_now_iso(), "timeframe": timeframe, "symbols": {}}

        for logical, broker in symbol_map.items():
            all_bars: list[dict[str, Any]] = []
            pos = 0
            while len(all_bars) < max_bars:
                need = min(chunk, max_bars - len(all_bars))
                batch = self.collector.fetch_candles_from(broker, timeframe, need, from_pos=pos)
                if not batch:
                    break
                all_bars.extend(batch)
                pos += len(batch)
                if len(batch) < need:
                    break

            path = self._save_parquet(logical, timeframe, all_bars)
            results["symbols"][logical] = {
                "broker_symbol": broker,
                "bars": len(all_bars),
                "path": str(path),
                "first": all_bars[0]["time"] if all_bars else None,
                "last": all_bars[-1]["time"] if all_bars else None,
            }
            self.logger.info("History download %s %s: %d bars -> %s", logical, timeframe, len(all_bars), path)

        return results

    def update_incremental(self, symbol_map: dict[str, str], timeframe: str = "M5") -> dict[str, Any]:
        """Append new candles since last stored bar."""
        fresh_count = int(self.history_cfg.get("incremental_bars", 500))
        results: dict[str, Any] = {"timestamp": utc_now_iso(), "timeframe": timeframe, "symbols": {}}

        for logical, broker in symbol_map.items():
            existing = self.load(logical, timeframe)
            fresh = self.collector.fetch_candles(broker, timeframe, fresh_count)

            if existing is not None and not existing.empty:
                combined = pd.concat([existing, pd.DataFrame(fresh)], ignore_index=True)
            else:
                combined = pd.DataFrame(fresh)
            path = self.parquet_path(logical, timeframe)
            combined = self._normalize_frame(combined, source=path)
            combined = combined.drop_duplicates(subset=["time"], keep="last").sort_values("time")

            path = self._save_parquet(logical, timeframe, combined.to_dict("records"))
            results["symbols"][logical] = {
                "total_bars": len(combined),
                "new_bars": len(fresh),
                "path": str(path),
                "last": combined["time"].iloc[-1] if len(combined) else None,
            }
            self.logger.info("History update %s %s: total=%d", logical, timeframe, len(combined))

        return results

    def _read_parquet_safe(self, path: Path) -> pd.DataFrame | None:
        """Load parquet or remove corrupt files so the next update can rebuild."""
        if not path.exists():
            return None
        size = path.stat().st_size
        if size < _MIN_PARQUET_BYTES:
            self.logger.warning("Corrupt parquet (%d bytes), removing: %s", size, path)
            path.unlink(missing_ok=True)
            return None

        with _parquet_thread_lock(path):
            last_exc: Exception | None = None
            for attempt in range(_READ_RETRIES):
                try:
                    df = self._read_parquet_once(path)
                    if df is None or df.empty:
                        return None
                    return self._normalize_frame(df, source=path)
                except Exception as exc:
                    last_exc = exc
                    if attempt < _READ_RETRIES - 1:
                        time.sleep(0.03 * (2 ** attempt))
            self.logger.warning("Failed to read parquet %s: %s — removing", path, last_exc)
            path.unlink(missing_ok=True)
            return None

    def _read_parquet_once(self, path: Path) -> pd.DataFrame | None:
        """Read parquet via temp copy to avoid Windows lock races with writers."""
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as handle:
            tmp_path = Path(handle.name)
        try:
            shutil.copy2(path, tmp_path)
            return pd.read_parquet(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def load(self, logical_symbol: str, timeframe: str) -> pd.DataFrame | None:
        return self._read_parquet_safe(self.parquet_path(logical_symbol, timeframe))

    def serve_recent(self, logical_symbol: str, timeframe: str, count: int) -> list[dict[str, Any]]:
        """Serve recent candles from Parquet for loops (avoids hammering MT5)."""
        df = self.load(logical_symbol, timeframe)
        if df is None or df.empty:
            return []
        tail = df.tail(count)
        return tail.to_dict("records")

    def _save_parquet(self, logical_symbol: str, timeframe: str, bars: list[dict[str, Any]]) -> Path:
        path = self.parquet_path(logical_symbol, timeframe)
        if not bars:
            return path
        df = pd.DataFrame(bars)
        if df.empty:
            return path
        df = self._normalize_frame(df, source=path)
        if df.empty:
            return path
        df = df.drop_duplicates(subset=["time"], keep="last").sort_values("time")
        tmp = path.with_suffix(".parquet.tmp")

        with _parquet_thread_lock(path), _parquet_file_lock(path):
            last_err: OSError | None = None
            for attempt in range(_REPLACE_RETRIES):
                try:
                    df.to_parquet(tmp, index=False)
                    _atomic_replace(tmp, path)
                    return path
                except OSError as exc:
                    last_err = exc
                    tmp.unlink(missing_ok=True)
                    if attempt < _REPLACE_RETRIES - 1:
                        time.sleep(0.04 * (2 ** attempt))
                    else:
                        try:
                            df.to_parquet(path, index=False)
                            tmp.unlink(missing_ok=True)
                            return path
                        except OSError:
                            raise last_err from exc
            if last_err:
                raise last_err
        return path

    def _normalize_frame(self, df: pd.DataFrame, *, source: Path) -> pd.DataFrame:
        """Normalize shared history column types for live loops and replay."""
        if "time" not in df.columns:
            return df

        out = df.copy()
        out["time"] = pd.to_datetime(out["time"], utc=True, errors="coerce")
        bad = out["time"].isna()
        if bad.any():
            self.logger.warning(
                "Dropping %d history rows with invalid time values from %s",
                int(bad.sum()),
                source,
            )
            out = out.loc[~bad].copy()
        return out

    def status(self, symbol_map: dict[str, str]) -> dict[str, Any]:
        """Return history store status for health monitor."""
        timeframes = [self.config["mt5"]["timeframes"]["entry"], self.config["mt5"]["timeframes"]["bias"]]
        symbols_status: dict[str, Any] = {}
        for logical in symbol_map:
            symbols_status[logical] = {}
            for tf in timeframes:
                path = self.parquet_path(logical, tf)
                df = self._read_parquet_safe(path)
                if df is not None:
                    symbols_status[logical][tf] = {
                        "bars": len(df),
                        "size_mb": round(path.stat().st_size / 1_048_576, 2),
                        "last": df["time"].iloc[-1] if len(df) else None,
                    }
                else:
                    symbols_status[logical][tf] = {"bars": 0, "size_mb": 0, "last": None}
        return {"timestamp": utc_now_iso(), "history_dir": str(HISTORY_DIR), "symbols": symbols_status}
