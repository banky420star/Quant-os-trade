"""Swap rate model — overnight holding costs per symbol."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


def swap_rate_table(
    broker_specs_path: str | Path = "broker_symbol_specs.json",
) -> dict[str, dict[str, float]]:
    """Read broker symbol specifications and extract swap rates.

    Expects a JSON file produced by the broker-spec dump script.
    Returns ``{symbol: {swap_long, swap_short, swap_type}}``.
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
    """Estimated daily swap cost for a position.

    *side* ``\"BUY\"`` or ``\"SELL\"``.  Returns cost in account currency.
    """
    row = swap_table.get(symbol, {})
    if not row:
        return 0.0

    swap_rate = row.get(
        "swap_long" if side.upper() == "BUY" else "swap_short", 0.0
    )
    return float(swap_rate) * float(volume)
