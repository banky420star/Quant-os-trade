"""Broker-native risk — minimum-lot stop risk and required equity.

Uses ``mt5.order_calc_profit()`` to compute the exact stop-loss cost
for a symbol at its minimum lot, then derives the required account
size for a given risk cap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def min_lot_stop_risk(
    broker_specs_path: str | Path = "broker_symbol_specs.json",
    *,
    stop_distance_atr_multiple: float = 1.5,
    atr_lookup: dict[str, float] | None = None,
) -> dict[str, dict[str, float]]:
    """Estimate the loss at minimum lot for each symbol using broker specs.

    Returns ``{symbol: {volume_min, point, stop_loss_usd, ...}}``.

    For a definitive calculation use ``mt5.order_calc_profit()`` with a
    live quote — this is a planning estimate from static specifications.
    """
    import json

    path = Path(broker_specs_path)
    if not path.exists():
        return {}

    specs = json.loads(path.read_text(encoding="utf-8"))
    atr_lookup = atr_lookup or {}

    result: dict[str, dict[str, float]] = {}
    for sym, info in specs.items():
        if not isinstance(info, dict) or info.get("error"):
            continue

        vol_min = float(info.get("volume_min", 0.01))
        point = float(info.get("point", 0))
        contract = float(info.get("trade_contract_size", 0))
        tick_value = float(info.get("trade_tick_value", 0))
        tick_size = float(info.get("trade_tick_size", 0))

        # Estimate stop distance from ATR if available
        atr = float(atr_lookup.get(sym, 0))
        if atr <= 0 or point <= 0:
            stop_points = 0
        else:
            stop_points = (atr * stop_distance_atr_multiple) / point

        # Loss estimate: stop_points × tick_value / tick_size × volume
        if tick_size > 0 and tick_value > 0:
            loss_per_lot = (stop_points * tick_value / tick_size) * vol_min
        else:
            loss_per_lot = 0.0

        result[sym] = {
            "volume_min": vol_min,
            "volume_step": float(info.get("volume_step", 0.01)),
            "point": point,
            "contract_size": contract,
            "tick_value": tick_value,
            "tick_size": tick_size,
            "stop_distance_points_est": round(stop_points, 1),
            "min_lot_stop_loss_usd": round(loss_per_lot, 2),
        }

    return result


def required_equity(
    broker_risk: dict[str, dict[str, float]],
    risk_fraction: float,
) -> dict[str, float]:
    """Compute required account equity per symbol for a given risk cap.

    ``required_equity = min_lot_stop_loss / risk_fraction``

    *risk_fraction* is decimal (e.g. ``0.005`` for 0.5%).
    """
    if risk_fraction <= 0:
        return {}

    return {
        sym: round(info["min_lot_stop_loss_usd"] / risk_fraction, 2)
        for sym, info in broker_risk.items()
        if info.get("min_lot_stop_loss_usd", 0) > 0
    }
