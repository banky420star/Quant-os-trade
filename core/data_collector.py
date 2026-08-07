"""Data Collector — pull OHLCV candles from MT5. Read-only, no trades."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from core.mt5_connection_manager import MT5ConnectionManager
from core.symbol_manager import SymbolManager
from core.utils import utc_now_iso

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore

TIMEFRAME_MAP = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}


class DataCollector:
    """Collect candle data via Connection Manager + Symbol Manager."""

    def __init__(
        self,
        config: dict[str, Any],
        connection: MT5ConnectionManager,
        symbols: SymbolManager,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self.connection = connection
        self.symbols = symbols
        self.logger = logger or logging.getLogger("data_collector")

    def pull_latest(self) -> dict[str, Any]:
        """Pull recent candles for all configured symbols and timeframes."""
        if not self.connection.connected:
            raise RuntimeError("Not connected to MT5")

        mt5_cfg = self.config["mt5"]
        symbol_map = self.symbols.symbol_map
        if not symbol_map:
            broker_data = self.symbols.discover()
            symbol_map = broker_data["resolved"]

        entry_tf = mt5_cfg["timeframes"]["entry"]
        bias_tf = mt5_cfg["timeframes"]["bias"]
        count = int(mt5_cfg.get("candles", 300))

        # M1 is fetched ONLY when the M1 structure shadow engine is enabled.
        # M5/M15 (entry/bias) collection is unchanged.
        m1_cfg = self.config.get("m1_structure") or {}
        m1_tf = "M1" if bool(m1_cfg.get("enabled", False)) else None
        timeframes = (entry_tf, bias_tf) + ((m1_tf,) if m1_tf else ())

        result: dict[str, Any] = {
            "timestamp": utc_now_iso(),
            "source": "mt5",
            "symbol_map": dict(symbol_map),
            "account": self.connection.account_snapshot(),
            "symbols": {},
        }

        for logical, broker in symbol_map.items():
            payload: dict[str, Any] = {"broker_symbol": broker}
            # Primary timeframes are hard requirements — the pipeline depends on
            # them. M1 is SHADOW/optional: a missing or failed M1 pull must
            # never take down the primary M5/M15 data path, so it is best-effort.
            for tf in (entry_tf, bias_tf):
                candles = self.fetch_candles(broker, tf, count)
                payload[tf] = candles
                self.logger.info("Collected %d candles: %s (%s) %s", len(candles), logical, broker, tf)
            if m1_tf:
                try:
                    m1_candles = self.fetch_candles(broker, m1_tf, count)
                    payload[m1_tf] = m1_candles
                    self.logger.info("Collected %d candles: %s (%s) %s", len(m1_candles), logical, broker, m1_tf)
                except Exception as exc:  # noqa: BLE001 — M1 is shadow/optional
                    self.logger.warning("M1 collect skipped for %s (%s): %s", logical, broker, exc)
            result["symbols"][logical] = payload

        return result

    def fetch_candles(self, symbol: str, timeframe: str, count: int) -> list[dict[str, Any]]:
        """Fetch OHLCV bars for a broker symbol."""
        tf = self._resolve_timeframe(timeframe)
        if tf is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Cannot select {symbol}: {mt5.last_error()}")

        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"No rates for {symbol} {timeframe}: {mt5.last_error()}")

        return [self._bar_to_dict(bar) for bar in rates]

    def fetch_candles_from(self, symbol: str, timeframe: str, count: int, from_pos: int = 0) -> list[dict[str, Any]]:
        """Fetch candles starting at position (for history downloads)."""
        tf = self._resolve_timeframe(timeframe)
        if tf is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Cannot select {symbol}: {mt5.last_error()}")
        rates = mt5.copy_rates_from_pos(symbol, tf, from_pos, count)
        if rates is None:
            return []
        return [self._bar_to_dict(bar) for bar in rates]

    def _resolve_timeframe(self, timeframe: str) -> int | None:
        if mt5 is None:
            return None
        attr = TIMEFRAME_MAP.get(timeframe)
        return getattr(mt5, attr, None) if attr else None

    @staticmethod
    def _bar_to_dict(bar: Any) -> dict[str, Any]:
        return {
            "time": datetime.fromtimestamp(int(bar["time"]), tz=timezone.utc).isoformat(),
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "volume": float(bar["tick_volume"]),
        }