"""Strategy Optimizer — grid search parameters via replay."""

from __future__ import annotations

import copy
import itertools
import logging
from typing import Any

from core.decision_engine import DecisionEngine
from core.replay_engine import ReplayEngine
from core.utils import utc_now_iso, write_json_state


class StrategyOptimizer:
    """Run replay across parameter combinations and rank results."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("strategy_optimizer")
        self.opt_cfg = config.get("optimizer", {})

    def run(
        self,
        symbol: str | None = None,
        max_runs: int | None = None,
    ) -> dict[str, Any]:
        symbol = symbol or self.config.get("replay", {}).get("symbol")
        max_runs = max_runs or int(self.opt_cfg.get("max_runs", 12))
        grid = self._build_grid()
        results: list[dict[str, Any]] = []

        for i, params in enumerate(grid[:max_runs]):
            cfg = copy.deepcopy(self.config)
            cfg["signals"]["min_confidence"] = params["min_confidence"]
            weights = dict(DecisionEngine.SUBSYSTEM_WEIGHTS)
            weights["trend_engine"] = round(
                DecisionEngine.SUBSYSTEM_WEIGHTS["trend_engine"] * params["trend_weight_mult"],
                4,
            )
            cfg["optimizer_weights"] = weights

            replay = ReplayEngine(cfg, self.logger)
            try:
                out = replay.run(symbol=symbol)
                score = self._score(out)
                results.append({
                    "run": i + 1,
                    "params": params,
                    "score": score,
                    "pnl_total": out.get("pnl_total"),
                    "win_rate_pct": out.get("win_rate_pct"),
                    "trades_closed": out.get("trades_closed"),
                    "final_equity": out.get("final_equity"),
                })
                self.logger.info(
                    "Optimizer run %d: conf=%d trend_mult=%.1f score=%.2f pnl=%.2f",
                    i + 1,
                    params["min_confidence"],
                    params["trend_weight_mult"],
                    score,
                    out.get("pnl_total", 0),
                )
            except Exception as exc:
                self.logger.error("Optimizer run %d failed: %s", i + 1, exc)

        results.sort(key=lambda r: r["score"], reverse=True)
        best = results[0] if results else None
        output = {
            "timestamp": utc_now_iso(),
            "symbol": symbol,
            "runs": len(results),
            "best": best,
            "all_results": results,
        }
        write_json_state("optimizer_results.json", output)
        return output

    def _build_grid(self) -> list[dict[str, Any]]:
        confs = self.opt_cfg.get("min_confidence_grid", [60, 70, 80])
        mults = self.opt_cfg.get("trend_weight_mult_grid", [0.9, 1.0, 1.1])
        return [
            {"min_confidence": c, "trend_weight_mult": m}
            for c, m in itertools.product(confs, mults)
        ]

    @staticmethod
    def _score(replay_out: dict[str, Any]) -> float:
        pnl = float(replay_out.get("pnl_total", 0))
        wr = float(replay_out.get("win_rate_pct", 0))
        trades = int(replay_out.get("trades_closed", 0))
        if trades < 3:
            return pnl * 0.5
        return pnl + (wr - 50) * 0.5 + min(trades, 20) * 0.1