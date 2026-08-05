"""Backward-compatible MT5 client facade — delegates to infrastructure layer."""

from __future__ import annotations

import logging
from typing import Any

from core.data_collector import DataCollector
from core.mt5_connection_manager import MT5ConnectionManager
from core.mt5_owner import MT5Owner
from core.mt5_terminal_manager import MT5TerminalManager, get_python_session_id
from core.symbol_manager import SymbolManager, broker_symbol, load_resolved_symbol_map

# Re-export session helpers used by loops/tests
__all__ = [
    "MT5Client",
    "detect_session_alignment",
    "format_mt5_connection_error",
    "get_python_session_id",
    "get_mt5_terminal_processes",
    "log_session_alignment",
]

IPC_TIMEOUT_CODE = -10005


def detect_session_alignment() -> dict[str, Any]:
    from core.utils import load_config
    return MT5TerminalManager(load_config()).session_alignment()


def log_session_alignment(logger: logging.Logger) -> dict[str, Any]:
    info = detect_session_alignment()
    logger.info(
        "Session check: python_pid=%s python_session=%s mt5_processes=%s aligned=%s",
        info["python_pid"],
        info["python_session_id"],
        info["mt5_processes"],
        info["aligned"],
    )
    if info.get("warning"):
        logger.warning(info["warning"])
    return info


def format_mt5_connection_error(error: Any, session_info: dict[str, Any] | None = None) -> str:
    if isinstance(error, tuple) and error and error[0] == IPC_TIMEOUT_CODE:
        base = f"MT5 IPC timeout (code {error[0]}: {error[1]})."
        session_info = session_info or detect_session_alignment()
        if session_info.get("warning"):
            return f"{base} {session_info['warning']}"
        return f"{base} Ensure MT5 is in the same Windows session as Python."
    return str(error)


def get_mt5_terminal_processes() -> list[dict[str, Any]]:
    from core.utils import load_config
    return MT5TerminalManager(load_config()).list_processes()


class MT5Client:
    """Facade over Connection Manager + Symbol Manager + Data Collector."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None, force_mock: bool | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("mt5_client")
        self._connection = MT5ConnectionManager(config, logger)
        self._symbols = SymbolManager(config, logger)
        self._collector: DataCollector | None = None
        self._symbol_map: dict[str, str] = {}

    def connect(self) -> bool:
        self._connection.connect()
        self._load_symbol_map()
        self._collector = DataCollector(self.config, self._connection, self._symbols, self.logger)
        return True

    def _load_symbol_map(self) -> dict[str, str]:
        if self._symbol_map:
            return self._symbol_map
        resolved = load_resolved_symbol_map()
        if resolved:
            self._symbol_map = dict(resolved)
            self._symbols.set_resolved(resolved)
        return self._symbol_map

    def _broker_symbol(self, symbol: str) -> str:
        return broker_symbol(symbol, self._load_symbol_map())

    def disconnect(self) -> None:
        self._connection.disconnect()

    def discover_symbols(self, logical_symbols: list[str] | None = None) -> dict[str, str]:
        result = self._symbols.discover(logical_symbols)
        self._symbol_map = result["resolved"]
        return self._symbol_map

    def pull_all_candles(self) -> dict[str, Any]:
        if not self._collector:
            self._collector = DataCollector(self.config, self._connection, self._symbols, self.logger)
        return self._collector.pull_latest()

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[dict[str, Any]]:
        if not self._collector:
            self._collector = DataCollector(self.config, self._connection, self._symbols, self.logger)
        return self._collector.fetch_candles(symbol, timeframe, count)

    def get_current_price(self, symbol: str) -> dict[str, float] | None:
        if not self._connection.connected:
            return None
        owner = MT5Owner.instance()
        broker = self._broker_symbol(symbol)
        if not owner.symbol_select(broker, True):
            return None
        tick = owner.symbol_info_tick(broker)
        if tick is None:
            return None
        return {"bid": float(tick.bid), "ask": float(tick.ask), "mid": (tick.bid + tick.ask) / 2}

    def get_spread_points(self, symbol: str) -> float:
        if not self._connection.connected:
            return 0.0
        owner = MT5Owner.instance()
        broker = self._broker_symbol(symbol)
        if not owner.symbol_select(broker, True):
            return 0.0
        info = owner.symbol_info(broker)
        tick = owner.symbol_info_tick(broker)
        if info is None or tick is None or info.point == 0:
            return 0.0
        spread = (tick.ask - tick.bid) / info.point
        if spread > 0:
            return spread
        # Some brokers only populate symbol_info.spread when tick spread is zero.
        return float(getattr(info, "spread", 0) or 0)