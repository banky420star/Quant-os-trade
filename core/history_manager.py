"""History Manager — download, store Parquet, incremental updates, serve loops."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from core.data_collector import DataCollector
from core.utils import DATA_DIR, utc_now_iso

HISTORY_DIR = DATA_DIR / "history"


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
                combined = combined.drop_duplicates(subset=["time"], keep="last").sort_values("time")
            else:
                combined = pd.DataFrame(fresh)

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
        if size < 12:
            self.logger.warning("Corrupt parquet (%d bytes), removing: %s", size, path)
            path.unlink(missing_ok=True)
            return None
        try:
            df = pd.read_parquet(path)
            if df is None or df.empty:
                return None
            return df
        except Exception as exc:
            self.logger.warning("Failed to read parquet %s: %s — removing", path, exc)
            path.unlink(missing_ok=True)
            return None

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
        df = df.drop_duplicates(subset=["time"], keep="last").sort_values("time")
        tmp = path.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        tmp.replace(path)
        return path

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