"""Symbol Manager — auto-discover broker symbols, never hardcode names."""

from __future__ import annotations

import logging
import re
from typing import Any

from core.utils import read_json_state, utc_now_iso

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore

# Asset class -> broker name patterns (priority order)
ASSET_PATTERNS: dict[str, list[str]] = {
    "XAU": [r"^XAUUSDm$", r"^XAUUSD\.?$", r"^XAUUSD[a-z]?$", r"^GOLD", r"XAUUSD"],
    "OIL": [r"^USOILm$", r"^USOIL\.?$", r"^WTI$", r"^BRENT$", r"^UKOIL", r"^XTIUSD", r"USOIL", r"WTI", r"BRENT", r"OIL"],
    "BTC": [r"^BTCUSDm$", r"^BTCUSD\.?$", r"^BTCUSD[a-z]?$", r"BTCUSD"],
    "EURUSD": [r"^EURUSDm$", r"^EURUSD\.?$", r"^EURUSD[a-z]?$", r"EURUSD"],
    "GBPUSD": [r"^GBPUSDm$", r"^GBPUSD\.?$", r"^GBPUSD[a-z]?$", r"GBPUSD"],
    "USDJPY": [r"^USDJPYm$", r"^USDJPY\.?$", r"^USDJPY[a-z]?$", r"USDJPY"],
    "USDCHF": [r"^USDCHFm$", r"^USDCHF\.?$", r"^USDCHF[a-z]?$", r"USDCHF"],
    "AUDUSD": [r"^AUDUSDm$", r"^AUDUSD\.?$", r"^AUDUSD[a-z]?$", r"AUDUSD"],
    "US500": [r"^US500m$", r"^US500\.?$", r"^SPX500", r"^US500"],
    "US30": [r"^US30m$", r"^US30\.?$", r"^DJ30", r"^US30"],
    "NAS100": [r"^NAS100m$", r"^NAS100\.?$", r"^USTEC", r"^NDX", r"^NAS100"],
    "UK100": [r"^UK100m$", r"^UK100\.?$", r"^FTSE", r"^UK100"],
    "FR40": [r"^FR40m$", r"^FR40\.?$", r"^GER40$", r"^CAC40", r"^AUS200$", r"GER40", r"FR40", r"CAC"],
    "JP225": [r"^JP225m$", r"^JP225\.?$", r"^NI225", r"^JPN225", r"^N225", r"JP225", r"NI225"],
}

# Config logical key -> asset class
LOGICAL_ASSET_MAP: dict[str, str] = {
    "XAUUSDm": "XAU",
    "XAUUSD": "XAU",
    "USOILm": "OIL",
    "USOIL": "OIL",
    "BTCUSDm": "BTC",
    "BTCUSD": "BTC",
    "EURUSDm": "EURUSD",
    "EURUSD": "EURUSD",
    "GBPUSDm": "GBPUSD",
    "GBPUSD": "GBPUSD",
    "USDJPYm": "USDJPY",
    "USDJPY": "USDJPY",
    "USDCHFm": "USDCHF",
    "USDCHF": "USDCHF",
    "AUDUSDm": "AUDUSD",
    "AUDUSD": "AUDUSD",
    "US500m": "US500",
    "US500": "US500",
    "US30m": "US30",
    "US30": "US30",
    "NAS100m": "NAS100",
    "NAS100": "NAS100",
    "UK100m": "UK100",
    "UK100": "UK100",
    "FR40m": "FR40",
    "FR40": "FR40",
    "JP225m": "JP225",
    "JP225": "JP225",
}


class SymbolManager:
    """Discover and resolve broker symbols for configured assets."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("symbol_manager")
        self._map: dict[str, str] = {}
        self._candidates: dict[str, list[str]] = {}

    @property
    def symbol_map(self) -> dict[str, str]:
        return dict(self._map)

    def set_resolved(self, symbol_map: dict[str, str]) -> None:
        """Load a pre-resolved symbol map (e.g. from broker_symbols.json)."""
        self._map = dict(symbol_map)

    def discover(self, logical_symbols: list[str] | None = None) -> dict[str, Any]:
        """Discover broker symbols and return full broker_symbols payload."""
        if mt5 is None:
            raise RuntimeError("MetaTrader5 not available")

        logical_symbols = logical_symbols or self.config["mt5"]["symbols"]
        all_syms = mt5.symbols_get()
        if not all_syms:
            raise RuntimeError(f"Cannot list symbols: {mt5.last_error()}")

        names = sorted(s.name for s in all_syms)
        self.logger.info("Scanning %d broker symbols for %s", len(names), logical_symbols)

        resolved: dict[str, str] = {}
        candidates: dict[str, list[str]] = {}

        for logical in logical_symbols:
            asset = LOGICAL_ASSET_MAP.get(logical, logical.replace("m", "").replace("USD", ""))
            patterns = ASSET_PATTERNS.get(asset, [logical])
            matches = self._find_matches(patterns, names)
            candidates[logical] = matches

            broker = self._pick_best(matches)
            if broker:
                resolved[logical] = broker
                self.logger.info("Resolved %s -> %s (candidates: %s)", logical, broker, matches[:5])
            else:
                self.logger.error("Failed to resolve %s. Candidates: %s", logical, matches[:10])

        self._map = resolved
        self._candidates = candidates
        missing = [sym for sym in logical_symbols if sym not in resolved]
        if missing:
            self.logger.warning(
                "Symbol discovery partial: resolved %d/%d, missing=%s",
                len(resolved),
                len(logical_symbols),
                missing,
            )

        return {
            "timestamp": utc_now_iso(),
            "logical_symbols": logical_symbols,
            "resolved": resolved,
            "candidates": {k: v[:10] for k, v in candidates.items()},
            "total_broker_symbols": len(names),
            "missing": missing,
        }

    def _find_matches(self, patterns: list[str], names: list[str]) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for pattern in patterns:
            regex = re.compile(pattern, re.IGNORECASE)
            for name in names:
                if regex.search(name) and name not in seen:
                    seen.add(name)
                    found.append(name)
        return found

    def _pick_best(self, candidates: list[str]) -> str | None:
        for name in candidates:
            if self._has_live_data(name):
                return name
        return candidates[0] if candidates else None

    def _has_live_data(self, symbol: str) -> bool:
        if not mt5.symbol_select(symbol, True):
            return False
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 1)
        return rates is not None and len(rates) > 0


def load_resolved_symbol_map() -> dict[str, str]:
    """Logical -> broker symbol map from the latest data_loop discovery."""
    broker = read_json_state("broker_symbols.json", default={}) or {}
    return dict(broker.get("resolved") or {})


def broker_symbol(logical: str, symbol_map: dict[str, str] | None = None) -> str:
    symbol_map = symbol_map if symbol_map is not None else load_resolved_symbol_map()
    return symbol_map.get(logical, logical)


def logical_symbol(name: str, symbol_map: dict[str, str] | None = None) -> str:
    symbol_map = symbol_map if symbol_map is not None else load_resolved_symbol_map()
    if name in symbol_map:
        return name
    for logical, broker in symbol_map.items():
        if broker == name:
            return logical
    return name
