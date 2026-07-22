"""Sweep performance gates via portfolio replay; pick best projected monthly PnL."""

from __future__ import annotations

import copy
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.performance_benchmark import apply_benchmark_config, run_multi_symbol_benchmark
from core.utils import load_config

SCRATCH = Path(r"C:\Users\ADMINI~1\AppData\Local\Temp\3\grok-goal-5d7c28d4f228\implementer")


def main() -> int:
    base = load_config()
    grid = {
        "min_trade_score": [65, 68, 72],
        "min_confidence": [58, 62, 66],
        "default_risk_percent": [0.35, 0.45, 0.55],
    }
    combos = list(itertools.product(
        grid["min_trade_score"],
        grid["min_confidence"],
        grid["default_risk_percent"],
    ))
    best: dict | None = None
    results: list[dict] = []

    for min_score, min_conf, risk_pct in combos:
        cfg = copy.deepcopy(base)
        perf = cfg.setdefault("performance", {})
        perf["min_trade_score"] = min_score
        perf["min_trade_score_aggressive"] = min_score
        perf["min_confidence"] = min_conf
        perf["default_risk_percent"] = risk_pct
        cfg = apply_benchmark_config(cfg)
        out = run_multi_symbol_benchmark(
            cfg,
            max_bars=3000,
            step=10,
        )
        row = {
            "params": {
                "min_trade_score": min_score,
                "min_confidence": min_conf,
                "default_risk_percent": risk_pct,
            },
            "projected_monthly_pnl": out["projected_monthly_pnl"],
            "meets_target": out["meets_target"],
            "coverage_ok": out["coverage_ok"],
            "pnl_ok": out["pnl_ok"],
            "trades_closed": out["portfolio"]["trades_closed"],
            "pnl_total": out["portfolio"]["pnl_total"],
            "replay_window_days": out["projection"]["replay_window_days"],
        }
        results.append(row)
        print(json.dumps(row))
        if out["meets_target"] and (best is None or out["projected_monthly_pnl"] > best["projected_monthly_pnl"]):
            best = {**row, "full": out}

    results.sort(key=lambda r: (r["meets_target"], r["projected_monthly_pnl"]), reverse=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    payload = {"best": best["params"] if best else None, "top5": results[:5], "all": results}
    out_path = SCRATCH / "tune_performance.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {out_path}")
    if best:
        print(f"BEST: {json.dumps(best['params'])} monthly={best['projected_monthly_pnl']} meets={best['meets_target']}")
    return 0 if best and best["meets_target"] else 1


if __name__ == "__main__":
    raise SystemExit(main())