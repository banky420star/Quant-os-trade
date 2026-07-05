"""Per-setup strategy evaluator — replay each of the 8 arena setups in isolation.

Runs the live pipeline (FeatureEngine → DecisionEngine → Verifier → PaperBroker)
with ``replay.only_setup_type`` set so each setup is measured independently.
Output: ``state/setup_eval_report.json`` for optimization / culturing.

Usage:
    python scripts/setup_evaluator.py --quick
    python scripts/setup_evaluator.py --symbols XAUUSDm USOILm
    python scripts/setup_evaluator.py --setups mean_reversion range_fade
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.replay_engine import run_portfolio_replay  # noqa: E402
from core.setup_triggers import SETUP_ORDER, export_catalog, trigger_rules  # noqa: E402
from core.utils import load_config, setup_logger, write_json_state  # noqa: E402

LIVE_SPREAD_POINTS = {
    "XAUUSDm": 240.0,
    "USOILm": 20.0,
    "BTCUSDm": 1000.0,
    "US500m": 80.0,
    "NAS100m": 100.0,
    "JP225m": 100.0,
}


def _score_row(trades: int, wr: float, pnl: float, exp_r: float) -> float:
    if trades < 5:
        return pnl * 0.25
    return pnl + (wr - 50) * 0.3 + exp_r * 20 + min(trades, 40) * 0.05


def evaluate_setup(
    base_config: dict,
    setup_type: str,
    symbols: list[str],
    *,
    max_bars: int,
    logger,
) -> dict:
    cfg = copy.deepcopy(base_config)
    cfg.setdefault("replay", {})["only_setup_type"] = setup_type
    cfg["replay"]["max_bars"] = max_bars
    cfg["execution"]["mode"] = "paper"
    cfg["execution"]["live_trading_enabled"] = False
    cfg["execution"]["mt5_trading_enabled"] = False
    cfg.setdefault("quant", {})["replay_ingest_edge_db"] = False
    cfg.setdefault("mt5", {})["symbols"] = list(symbols)

    spread = {s: LIVE_SPREAD_POINTS.get(s, 50.0) for s in symbols}
    try:
        out = run_portfolio_replay(
            cfg,
            symbols=symbols,
            spread_data=spread,
            return_all_trades=True,
        )
    except Exception as exc:
        logger.error("Setup %s replay failed: %s", setup_type, exc)
        return {
            "setup_type": setup_type,
            "error": str(exc),
            "trigger_rules": trigger_rules(setup_type),
            "score": -999.0,
        }

    trades = out.get("trades") or []
    sym_trades = [t for t in trades if t.get("setup_type") == setup_type]
    wins = sum(1 for t in sym_trades if t.get("result") == "win" or float(t.get("pnl", 0)) > 0)
    n = len(sym_trades)
    wr = (100.0 * wins / n) if n else 0.0
    pnl = sum(float(t.get("pnl", 0)) for t in sym_trades)
    rs = []
    for t in sym_trades:
        entry = float(t.get("entry") or 0)
        exit_p = float(t.get("exit") or t.get("exit_price") or entry)
        sl = float(t.get("sl") or 0)
        side = str(t.get("side", "BUY")).upper()
        if entry > 0 and sl > 0:
            risk = abs(entry - sl)
            if risk > 0:
                move = (exit_p - entry) if side == "BUY" else (entry - exit_p)
                rs.append(move / risk)
    exp_r = sum(rs) / len(rs) if rs else 0.0

    return {
        "setup_type": setup_type,
        "trigger_rules": trigger_rules(setup_type),
        "trades_closed": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate_pct": round(wr, 1),
        "pnl_total": round(pnl, 2),
        "expectancy_r": round(exp_r, 4),
        "score": round(_score_row(n, wr, pnl, exp_r), 2),
        "bars_replayed": out.get("bars_replayed"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Evaluate each arena setup in isolation")
    ap.add_argument("--quick", action="store_true", help="Use 400 bars (fast smoke)")
    ap.add_argument("--max-bars", type=int, default=None)
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--setups", nargs="*", default=None, help="Subset of setup types")
    ap.add_argument("--out", default="setup_eval_report.json")
    args = ap.parse_args()

    logger = setup_logger("setup_evaluator", "setup_evaluator.log")
    config = load_config()
    catalog = export_catalog(config)

    symbols = args.symbols or list(
        (config.get("strategy_arena") or {}).get("symbols")
        or config.get("mt5", {}).get("symbols")
        or ["XAUUSDm"]
    )
    setups = args.setups or list(SETUP_ORDER)
    max_bars = args.max_bars or (400 if args.quick else int(config.get("replay", {}).get("max_bars", 800)))

    logger.info("Setup evaluator: %d setups × %d symbols × %d bars", len(setups), len(symbols), max_bars)
    rows = []
    for setup in setups:
        logger.info("Evaluating setup: %s", setup)
        rows.append(evaluate_setup(config, setup, symbols, max_bars=max_bars, logger=logger))

    rows.sort(key=lambda r: float(r.get("score", -999)), reverse=True)
    report = {
        "catalog": catalog,
        "symbols": symbols,
        "max_bars": max_bars,
        "results": rows,
        "ranked": [r["setup_type"] for r in rows if not r.get("error")],
        "best": rows[0] if rows else None,
    }
    write_json_state(args.out, report)
    print(json.dumps({"best": report.get("best"), "ranked": report["ranked"][:5]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())