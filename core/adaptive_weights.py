"""Adaptive Weight Optimizer — learn subsystem weights from edge database."""

from __future__ import annotations

import logging
from typing import Any

from core.edge_database import EdgeDatabase
from core.utils import read_json_state, utc_now_iso, write_json_state
from core.weight_defaults import SUBSYSTEM_WEIGHTS

DEFAULT_WEIGHTS = dict(SUBSYSTEM_WEIGHTS)


def load_weights(config: dict[str, Any], symbol: str | None = None) -> dict[str, float]:
    """Load active weights: deployed adaptive > config override > defaults."""
    override = config.get("optimizer_weights") or config.get("_replay_weights")
    if override:
        weights = dict(DEFAULT_WEIGHTS)
        weights.update(override)
        return weights

    quant = config.get("quant", {})
    if not quant.get("use_adaptive_weights", True):
        return dict(DEFAULT_WEIGHTS)

    state = read_json_state("adaptive_weights.json", default={})
    if symbol and state.get("deployed"):
        per_sym = state.get("per_symbol") or {}
        sym_doc = per_sym.get(symbol) or {}
        if sym_doc.get("weights"):
            return dict(sym_doc["weights"])

    if state.get("deployed") and state.get("weights"):
        return dict(state["weights"])
    return dict(DEFAULT_WEIGHTS)


class AdaptiveWeightOptimizer:
    """Derive subsystem weights from winning vs losing trade evidence."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("adaptive_weights")
        self.edge_db = EdgeDatabase(logger)

    def optimize(self, min_trades: int = 15) -> dict[str, Any]:
        """Compute candidate weights from edge database; does not auto-deploy."""
        records = self.edge_db.load().get("records", [])
        if len(records) < min_trades:
            return {
                "timestamp": utc_now_iso(),
                "status": "insufficient_data",
                "trade_count": len(records),
                "min_trades": min_trades,
                "weights": dict(DEFAULT_WEIGHTS),
                "deployed": False,
            }

        engines = list(DEFAULT_WEIGHTS.keys())
        win_scores = {e: 0.0 for e in engines}
        loss_scores = {e: 0.0 for e in engines}
        win_n = loss_n = 0

        for rec in records:
            tree = rec.get("confidence_tree") or {}
            if not tree:
                continue
            if rec.get("result") == "win":
                win_n += 1
                for e in engines:
                    win_scores[e] += float(tree.get(e, 50))
            elif rec.get("result") == "loss":
                loss_n += 1
                for e in engines:
                    loss_scores[e] += float(tree.get(e, 50))

        raw: dict[str, float] = {}
        for e in engines:
            win_avg = (win_scores[e] / win_n) if win_n else 50
            loss_avg = (loss_scores[e] / loss_n) if loss_n else 50
            edge = max(0.0, win_avg - loss_avg * 0.5)
            raw[e] = edge + 10.0

        total = sum(raw.values()) or 1.0
        new_weights = {e: round(raw[e] / total, 4) for e in engines}

        baseline_total = sum(DEFAULT_WEIGHTS.values())
        normalized_baseline = {e: round(DEFAULT_WEIGHTS[e] / baseline_total, 4) for e in engines}

        deltas = {e: round((new_weights[e] - normalized_baseline[e]) * 100, 2) for e in engines}

        result = {
            "timestamp": utc_now_iso(),
            "status": "candidate",
            "trade_count": len(records),
            "wins_analyzed": win_n,
            "losses_analyzed": loss_n,
            "weights": new_weights,
            "baseline_weights": normalized_baseline,
            "deltas_pct": deltas,
            "deployed": False,
            "method": "win_loss_engine_correlation",
        }
        self.logger.info(
            "Adaptive weights candidate: trend=%.3f structure=%.3f (from %d trades)",
            new_weights.get("trend_engine", 0),
            new_weights.get("structure_engine", 0),
            len(records),
        )
        return result

    def reward_weighted_weights(self, min_trades: int = 15) -> dict[str, Any]:
        """Candidate weights from REALIZED-PROFIT credit assignment.

        Unlike ``optimize`` (which credits engines by win/loss counts on their
        confidence), this weights each engine's confidence contribution by the
        realized R-multiple of the trade — so an engine that was confident on
        big winners is rewarded in proportion to the money made, and confidence
        on big losers is penalized proportionally. See core/reward_engine.py.

        Never auto-deploys; returns a candidate for the same replay-gated
        deploy path as ``optimize``.
        """
        from core.reward_engine import credit_to_weights, engine_credit, reward_score

        records = self.edge_db.load().get("records", [])
        engines = list(DEFAULT_WEIGHTS.keys())
        baseline_total = sum(DEFAULT_WEIGHTS.values())
        normalized_baseline = {e: round(DEFAULT_WEIGHTS[e] / baseline_total, 4) for e in engines}

        if len(records) < min_trades:
            return {
                "timestamp": utc_now_iso(),
                "status": "insufficient_data",
                "trade_count": len(records),
                "min_trades": min_trades,
                "weights": dict(normalized_baseline),
                "deployed": False,
                "method": "reward_weighted_credit",
            }

        credit = engine_credit(records, engines=engines)
        new_weights = credit_to_weights(credit, baseline=DEFAULT_WEIGHTS)
        reward = reward_score(records)
        deltas = {e: round((new_weights[e] - normalized_baseline[e]) * 100, 2) for e in engines}

        self.logger.info(
            "Reward-weighted candidate: expectancy=%.3fR score=%.2f (from %d trades)",
            reward.get("expectancy_r", 0.0),
            reward.get("score", 0.0),
            len(records),
        )
        return {
            "timestamp": utc_now_iso(),
            "status": "candidate",
            "trade_count": len(records),
            "weights": new_weights,
            "baseline_weights": normalized_baseline,
            "deltas_pct": deltas,
            "engine_credit": credit,
            "reward": reward,
            "deployed": False,
            "method": "reward_weighted_credit",
        }

    def propose_deployment(
        self,
        candidate: dict[str, Any],
        replay_improvement_pct: float,
        threshold_pct: float = 5.0,
    ) -> dict[str, Any]:
        """Mark candidate as proposed if replay beat baseline by threshold."""
        deploy = replay_improvement_pct >= threshold_pct and candidate.get("status") == "candidate"
        proposal = {
            **candidate,
            "replay_improvement_pct": round(replay_improvement_pct, 2),
            "threshold_pct": threshold_pct,
            "proposal": "deploy" if deploy else "hold",
            "deployed": False,
            "message": (
                f"Candidate beats baseline by {replay_improvement_pct:.1f}% — ready for manual deploy"
                if deploy
                else f"Improvement {replay_improvement_pct:.1f}% below threshold {threshold_pct}%"
            ),
        }
        write_json_state("weight_candidates.json", proposal)
        return proposal

    @staticmethod
    def deploy_weights(candidate: dict[str, Any]) -> dict[str, Any]:
        """Manually deploy candidate weights (research loop never auto-deploys)."""
        deployed = {
            "timestamp": utc_now_iso(),
            "deployed": True,
            "weights": candidate.get("weights", DEFAULT_WEIGHTS),
            "source": candidate.get("method", "adaptive_optimizer"),
            "trade_count": candidate.get("trade_count", 0),
        }
        per_symbol = candidate.get("per_symbol")
        if isinstance(per_symbol, dict):
            sym_deployed = {}
            for sym, doc in per_symbol.items():
                if not isinstance(doc, dict) or doc.get("proposal") != "deploy":
                    continue
                if doc.get("weights"):
                    sym_deployed[sym] = {
                        "weights": doc["weights"],
                        "improvement_pct": doc.get("improvement_pct"),
                        "expectancy_usd": doc.get("candidate_expectancy_usd"),
                    }
            if sym_deployed:
                deployed["per_symbol"] = sym_deployed
        write_json_state("adaptive_weights.json", deployed)
        return deployed