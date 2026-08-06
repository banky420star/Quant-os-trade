"""Broker ↔ logical symbol mapping and renaming history.

MT5 broker symbols may differ from the internal logical name (e.g.
``XAUUSD`` vs ``XAUUSDm``).  This module maintains the mapping and
records renames so historical data can be stitched.
"""

from __future__ import annotations

import json
from typing import Any

from core.utils import read_json_state, write_json_state

SYMBOL_MAP_PATH = "symbol_map.json"


def _load() -> dict[str, Any]:
    """Load the persisted symbol mapping state.
    
    Returns:
        dict[str, Any]: The stored mapping data, or an empty dictionary when no state is available.
    """
    return read_json_state(SYMBOL_MAP_PATH, default={}) or {}


def _save(data: dict[str, Any]) -> None:
    """Persist symbol mapping data to the configured JSON state file.
    
    Parameters:
    	data (dict[str, Any]): Mapping state to persist.
    """
    write_json_state(SYMBOL_MAP_PATH, data)


def broker_to_logical(broker_symbol: str) -> str | None:
    """
    Map a broker symbol to its internal logical symbol.
    
    Returns:
    	str | None: The associated logical symbol, or `None` if no mapping exists.
    """
    data = _load()
    mapping: dict[str, str] = data.get("broker_to_logical", {})
    return mapping.get(broker_symbol)


def logical_to_broker(logical_symbol: str) -> str | None:
    """
    Map an internal logical symbol to its broker symbol.
    
    Parameters:
        logical_symbol (str): The logical symbol to look up.
    
    Returns:
        str | None: The corresponding broker symbol, or `None` if no mapping exists.
    """
    data = _load()
    mapping: dict[str, str] = data.get("logical_to_broker", {})
    return mapping.get(logical_symbol)


def register_symbol_pair(broker: str, logical: str) -> None:
    """Register a broker symbol and its corresponding logical symbol."""
    data = _load()
    data.setdefault("broker_to_logical", {})[broker] = logical
    data.setdefault("logical_to_broker", {})[logical] = broker
    _save(data)


def record_rename(old_logical: str, new_logical: str) -> None:
    """
    Record a logical symbol rename for historical data tracking.
    
    Parameters:
    	old_logical (str): The previous logical symbol.
    	new_logical (str): The replacement logical symbol.
    """
    data = _load()
    renames: list[dict[str, str]] = data.setdefault("renames", [])
    renames.append(
        {"old": old_logical, "new": new_logical, "timestamp": ""}
    )
    _save(data)


def rename_history() -> list[dict[str, str]]:
    """Return the list of recorded renames."""
    data = _load()
    return list(data.get("renames", []))
