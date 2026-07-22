"""Trade-log cell expectancy — setup×session buckets from state/trade_log.json."""

from __future__ import annotations

import statistics
from typing import Any

from core.strategy_policy import culturing_cell_from_trade
from core.utils import read_json_state


# Proven session-edge targets for candidate-3 evaluation.
EDGE_TARGETS: tuple[dict[str, Any], ...] = (
    {"symbol": "XAUUSDm", "setup": "pullback", "sessions": ["rollover", "tokyo"]},
    {"symbol": "USOILm", "setup": "pullback", "sessions": ["tokyo"]},
    {"symbol": "UK100m", "setup": "trend_continuation", "sessions": ["london_open"]},
    {"symbol": "UK100m", "setup": "pullback", "sessions": ["london_open"]},
)


def _cell_from_trade(trade: dict[str, Any]) -> str:
    cell = trade.get("culturing_cell")
    if cell:
        return str(cell)
    mapped = {
        "setup_type": trade.get("setup") or trade.get("setup_type"),
        "side": trade.get("side"),
        "market_context": {
            "session": trade.get("session"),
            "market_regime": {
                "primary": trade.get("regime_primary"),
                "bias": trade.get("regime_bias"),
            },
        },
    }
    return culturing_cell_from_trade(mapped)


def _summarize_cell(trades: list[dict[str, Any]]) -> dict[str, Any]:
    clean = [t for t in trades if not t.get("archive_polluted")]
    n = len(clean)
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0, "expectancy_r": 0.0, "total_pnl": 0.0}
    wins = sum(1 for t in clean if t.get("won") or t.get("result") == "win")
    losses = n - wins
    rs = [float(t["r_multiple"]) for t in clean if t.get("r_multiple") is not None]
    pnls = [float(t.get("pnl") or 0) for t in clean]
    exp_r = round(statistics.mean(rs), 4) if rs else 0.0
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100.0 * wins / n, 1),
        "expectancy_r": exp_r,
        "total_pnl": round(sum(pnls), 2),
    }


def bucket_trades_by_cell(
    trades: list[dict[str, Any]],
    *,
    symbols: list[str] | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Group trades by symbol -> culturing cell."""
    sym_filter = set(symbols or [])
    buckets: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for trade in trades:
        if trade.get("archive_polluted"):
            continue
        sym = str(trade.get("symbol") or "")
        if sym_filter and sym not in sym_filter:
            continue
        cell = _cell_from_trade(trade)
        if not cell:
            continue
        buckets.setdefault(sym, {}).setdefault(cell, []).append(trade)
    return buckets


def cell_expectancy_report(
    *,
    symbols: list[str] | None = None,
    trade_log: dict[str, Any] | None = None,
    targets: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """Expectancy by culturing cell from trade_log.json."""
    doc = trade_log if trade_log is not None else (read_json_state("trade_log.json", default={}) or {})
    trades = list(doc.get("trades") or doc.get("session_trades") or [])
    sym_list = list(symbols or [])
    buckets = bucket_trades_by_cell(trades, symbols=sym_list or None)

    per_symbol: dict[str, Any] = {}
    all_cells: list[dict[str, Any]] = []
    for sym, cells in sorted(buckets.items()):
        sym_rows: list[dict[str, Any]] = []
        for cell, cell_trades in sorted(cells.items()):
            stats = _summarize_cell(cell_trades)
            row = {"symbol": sym, "cell": cell, **stats}
            sym_rows.append(row)
            all_cells.append(row)
        sym_rows.sort(key=lambda r: (-float(r.get("expectancy_r", 0)), -int(r.get("n", 0))))
        per_symbol[sym] = sym_rows

    edge_targets = targets or EDGE_TARGETS
    target_rows: list[dict[str, Any]] = []
    for target in edge_targets:
        sym = str(target.get("symbol") or "")
        setup = str(target.get("setup") or "")
        for session in target.get("sessions") or []:
            matches = [
                r for r in all_cells
                if r["symbol"] == sym
                and r["cell"].startswith(f"{setup}|")
                and r["cell"].endswith(f"|{session}")
            ]
            if matches:
                best = max(matches, key=lambda r: (r["n"], r["expectancy_r"]))
                target_rows.append({**best, "target": f"{sym} {setup}@{session}"})
            else:
                target_rows.append({
                    "target": f"{sym} {setup}@{session}",
                    "symbol": sym,
                    "cell": f"{setup}|?|align|{session}",
                    "n": 0,
                    "expectancy_r": 0.0,
                    "win_rate_pct": 0.0,
                    "status": "no_data",
                })

    positive_targets = sum(1 for r in target_rows if float(r.get("expectancy_r", 0)) > 0 and int(r.get("n", 0)) > 0)
    return {
        "trade_count": len([t for t in trades if not t.get("archive_polluted")]),
        "symbols": sym_list or sorted(buckets.keys()),
        "per_symbol_cells": per_symbol,
        "edge_targets": target_rows,
        "positive_edge_targets": positive_targets,
        "edge_target_total": len(target_rows),
        "positive_evolution": positive_targets >= 2,
    }