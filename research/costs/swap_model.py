"""Swap rate model — overnight holding costs per symbol."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


def swap_rate_table(
    broker_specs_path: str | Path = "broker_symbol_specs.json",
) -> dict[str, dict[str, float]]:
    """
    Load symbol swap specifications from a broker-generated JSON file.
    
    Parameters:
    	broker_specs_path (str | Path): Path to the JSON file containing broker symbol specifications.
    
    Returns:
    	dict[str, dict[str, float]]: Mapping of symbols to long and short swap rates and swap type. Missing files produce an empty mapping; invalid entries are skipped.
    """
    import json

    path = Path(broker_specs_path)
    if not path.exists():
        return {}

    specs = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, dict[str, float]] = {}
    for sym, info in specs.items():
        if not isinstance(info, dict):
            continue
        result[sym] = {
            "swap_long": float(info.get("swap_long", 0) or 0),
            "swap_short": float(info.get("swap_short", 0) or 0),
            "swap_type": str(info.get("swap_type", "")),
        }
    return result


def daily_swap_cost(
    swap_table: dict[str, dict[str, float]],
    symbol: str,
    side: str,
    volume: float,
) -> float:
    """
    Estimate the daily holding cost for a position.
    
    Parameters:
        swap_table (dict[str, dict[str, float]]): Swap rates indexed by symbol.
        symbol (str): Symbol for the position.
        side (str): Position side; ``"BUY"`` selects the long rate, and other
            values select the short rate.
        volume (float): Position volume.
    
    Returns:
        float: Estimated daily swap cost in account currency, or ``0.0`` when the
            symbol is absent from the table.
    """
    row = swap_table.get(symbol, {})
    if not row:
        return 0.0

    swap_rate = row.get(
        "swap_long" if side.upper() == "BUY" else "swap_short", 0.0
    )
    return float(swap_rate) * float(volume)
