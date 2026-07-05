"""Research Loop — analyze edge DB, propose weights, validate via replay (no auto-deploy)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.adaptive_weights import AdaptiveWeightOptimizer
from core.edge_database import EdgeDatabase
from core.replay_engine import ReplayEngine
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state


def _replay_score(replay_out: dict) -> float:
    pnl = float(replay_out.get("pnl_total", 0))
    wr = float(replay_out.get("win_rate_pct", 0))
    trades = int(replay_out.get("trades_closed", 0))
    return pnl + (wr - 50) * 0.3 + min(trades, 30) * 0.05


def _find_patterns(db: EdgeDatabase) -> list[dict]:
    """Surface high/low edge patterns from aggregates."""
    aggregates = db.load().get("aggregates", {})
    patterns: list[dict] = []

    for ctx_key, setups in aggregates.get("by_context", {}).items():
        parts = ctx_key.split("|")
        if len(parts) < 3:
            continue
        symbol, regime, session = parts[0], parts[1], parts[2]
        for setup, stats in setups.items():
            if stats.get("total", 0) < 5:
                continue
            wr = stats.get("win_rate_pct", 0)
            if wr >= 60:
                patterns.append({
                    "type": "high_edge",
                    "setup": setup,
                    "symbol": symbol,
                    "regime": regime,
                    "session": session,
                    "win_rate_pct": wr,
                    "total": stats["total"],
                    "insight": f"{setup} in {session} {regime} on {symbol}: {wr}% ({stats['wins']}/{stats['total']})",
                })
            elif wr <= 35:
                patterns.append({
                    "type": "low_edge",
                    "setup": setup,
                    "symbol": symbol,
                    "regime": regime,
                    "session": session,
                    "win_rate_pct": wr,
                    "total": stats["total"],
                    "insight": f"Avoid {setup} in {session} {regime} on {symbol}: {wr}% ({stats['wins']}/{stats['total']})",
                })

    patterns.sort(key=lambda p: abs(p.get("win_rate_pct", 50) - 50), reverse=True)
    return patterns[:20]


def _research_summary(db_state: dict, patterns: list[dict]) -> dict:
    aggregates = db_state.get("aggregates", {}) if isinstance(db_state, dict) else {}
    by_context = aggregates.get("by_context", {}) if isinstance(aggregates, dict) else {}
    by_setup = aggregates.get("by_setup", {}) if isinstance(aggregates, dict) else {}
    return {
        "records": len(db_state.get("records", [])) if isinstance(db_state, dict) else 0,
        "context_cells": len(by_context) if isinstance(by_context, dict) else 0,
        "setup_cells": len(by_setup) if isinstance(by_setup, dict) else 0,
        "pattern_count": len(patterns),
    }


def run() -> dict:
    """
    Research cycle:
      1. Read edge database
      2. Find statistical patterns
      3. Optimize adaptive weights (candidate only)
      4. Replay-test baseline vs candidate
      5. Propose deployment if improvement > threshold (never auto-deploy)
    """
    config = load_config()
    logger = setup_logger("research_loop", "research_loop.log")
    research_cfg = config.get("research", {})
    logger.info("=== Research Loop starting ===")

    edge_db = EdgeDatabase(logger)
    db_state = edge_db.load()
    trade_count = len(db_state.get("records", []))
    min_trades = int(research_cfg.get("min_trades_for_research", 15))

    patterns = _find_patterns(edge_db)
    summary = _research_summary(db_state, patterns)
    status = "ok" if trade_count >= min_trades else "insufficient_data"
    if status != "ok":
        status_reason = f"waiting_for_more_trades:{trade_count}/{min_trades}"
    else:
        status_reason = "ready"
    report = {
        "timestamp": utc_now_iso(),
        "status": status,
        "status_reason": status_reason,
        "edge_records": trade_count,
        "summary": summary,
        "min_trades": min_trades,
        "patterns": patterns,
    }
    write_json_state("research_report.json", report)
    logger.info("Research: %d edge records, %d patterns found", trade_count, len(patterns))

    optimizer = AdaptiveWeightOptimizer(config, logger)
    candidate = optimizer.optimize(min_trades=min_trades)
    write_json_state("adaptive_weights_candidate.json", candidate)

    validation = {
        "timestamp": utc_now_iso(),
        "status": candidate.get("status", "unknown"),
        "trade_count": candidate.get("trade_count", trade_count),
        "min_trades": candidate.get("min_trades", min_trades),
        "weights": candidate.get("weights", {}),
        "deployed": bool(candidate.get("deployed", False)),
        "method": candidate.get("method", "unknown"),
        "reason": "insufficient_data" if candidate.get("status") != "candidate" else "candidate_ready",
    }
    write_json_state("research_validation.json", validation)

    if candidate.get("status") != "candidate":
        logger.info("Research: insufficient trades for weight optimization (%d < %d)", trade_count, min_trades)
        logger.info("=== Research Loop complete ===")
        return {"report": report, "candidate": candidate, "validation": validation, "proposal": None}

    symbol = config.get("replay", {}).get("symbol")
    replay_bars = int(research_cfg.get("replay_bars", 400))
    replay_step = int(research_cfg.get("replay_step", 15))

    baseline_cfg = copy.deepcopy(config)
    baseline_replay = ReplayEngine(baseline_cfg, logger)
    baseline_out = baseline_replay.run(symbol=symbol, max_bars=replay_bars, step=replay_step)
    baseline_score = _replay_score(baseline_out)

    candidate_cfg = copy.deepcopy(config)
    candidate_cfg["optimizer_weights"] = candidate["weights"]
    candidate_replay = ReplayEngine(candidate_cfg, logger)
    candidate_out = candidate_replay.run(symbol=symbol, max_bars=replay_bars, step=replay_step)
    candidate_score = _replay_score(candidate_out)

    improvement = 0.0
    if abs(baseline_score) > 0.01:
        improvement = (candidate_score - baseline_score) / abs(baseline_score) * 100
    elif candidate_score > baseline_score:
        improvement = 100.0

    threshold = float(research_cfg.get("improvement_threshold_pct", 5))
    auto_deploy = bool(research_cfg.get("auto_deploy", False))
    proposal = optimizer.propose_deployment(candidate, improvement, threshold)

    validation.update({
        "baseline_score": round(baseline_score, 3),
        "candidate_score": round(candidate_score, 3),
        "improvement_pct": round(improvement, 2),
        "threshold_pct": threshold,
        "baseline_pnl": baseline_out.get("pnl_total"),
        "candidate_pnl": candidate_out.get("pnl_total"),
        "proposal": proposal.get("proposal"),
    })
    write_json_state("research_validation.json", validation)

    if auto_deploy and proposal.get("proposal") == "deploy":
        optimizer.deploy_weights(candidate)
        logger.warning("Research: auto_deploy=true — weights deployed (override in config to disable)")
    else:
        logger.info(
            "Research: candidate %s (improvement=%.1f%%, threshold=%.1f%%) — NOT auto-deployed",
            proposal.get("proposal"),
            improvement,
            threshold,
        )

    micro_result: dict | None = None
    try:
        from core.micro_quant_evolution import micro_evolution_enabled, run_micro_evolution

        if micro_evolution_enabled(config):
            logger.info("Research: running micro quant evolution (30-c2 profile)")
            micro_result = run_micro_evolution(config, logger)
            if micro_result.get("positive_evolution"):
                micro_candidate = micro_result.get("candidate") or {}
                validation["micro_evolution"] = {
                    "symbols_evolved": micro_result.get("symbols_evolved", 0),
                    "proposal": micro_candidate.get("proposal"),
                    "replay_improvement_pct": micro_candidate.get("replay_improvement_pct"),
                    "positive_evolution": True,
                }
                write_json_state("research_validation.json", validation)
                logger.info(
                    "Research: micro evolution positive — %d symbol(s) -> weight_candidates.json (hold deploy)",
                    micro_result.get("symbols_evolved", 0),
                )
            else:
                validation["micro_evolution"] = {
                    "positive_evolution": False,
                    "proposal": "hold",
                }
                write_json_state("research_validation.json", validation)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Research: micro quant evolution skipped: %s", exc)

    logger.info("=== Research Loop complete ===")
    return {
        "report": report,
        "candidate": candidate,
        "validation": validation,
        "proposal": proposal,
        "micro_evolution": micro_result,
    }


if __name__ == "__main__":
    run()
