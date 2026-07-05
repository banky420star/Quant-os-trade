"""Agent 3: Decision Loop — evidence-first candidate signals via Decision Engine."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.decision_engine import DecisionEngine
from core.setup_triggers import write_setup_catalog
from core.strategy_arena import (
    arena_enabled,
    leaderboard,
    load_arena,
    rebuild_analytics_from_outcomes,
    record_triggers,
    reset_arena,
)
from core.strategy_ranker import StrategyRanker
from core.utils import (
    fail_safe_missing,
    load_config,
    read_json_state,
    setup_logger,
    utc_now_iso,
    write_json_state,
)


def run() -> dict | None:
    """Create evidence-based candidate signals — no verification or execution."""
    config = load_config()
    logger = setup_logger("signal_loop", "signal_loop.log")
    logger.info("=== Decision Loop starting (evidence-first) ===")

    if fail_safe_missing("features.json", logger):
        return None
    if fail_safe_missing("market_context.json", logger):
        return None

    features = read_json_state("features.json")
    market_ctx = read_json_state("market_context.json")
    context_data = market_ctx.get("market_context", market_ctx)
    edge_scores = read_json_state("edge_scores.json", default={"setups": {}, "setup_stats": {}})

    if arena_enabled(config):
        if not read_json_state("strategy_arena.json", default={}).get("campaign_id"):
            reset_arena(config, campaign_id="arena-live")
        else:
            write_setup_catalog(config)
            arena_state = load_arena()
            analytics = arena_state.get("analytics") or {}
            has_cells = bool((analytics.get("condition_cells") or {}))
            has_outcomes = bool(arena_state.get("outcome_log"))
            if has_outcomes and not has_cells:
                rebuild_analytics_from_outcomes(arena_state)

    engine = DecisionEngine(config, logger)
    ranker = StrategyRanker(config, logger)
    candidates = engine.generate_candidates(features, context_data, edge_scores)
    arena_report = record_triggers(candidates, config, logger=logger) if arena_enabled(config) else {}

    strategy_rankings: dict[str, list] = {}
    for symbol, feat in features.get("symbols", {}).items():
        ctx = context_data.get("symbols", {}).get(symbol, {})
        strategy_rankings[symbol] = ranker.rank_for_symbol(symbol, ctx, feat)

    top_explain = candidates[0].get("explain") if candidates else None
    output = {
        "timestamp": utc_now_iso(),
        "engine": "decision_engine",
        "count": len(candidates),
        "candidates": candidates,
        "top_explain": top_explain,
        "strategy_rankings": strategy_rankings,
        "strategy_arena": leaderboard() if arena_enabled(config) else None,
        "arena_triggers_recorded": arena_report.get("recorded", 0),
    }
    write_json_state("candidate_signals.json", output)
    write_json_state("strategy_rankings.json", {
        "timestamp": utc_now_iso(),
        "rankings": strategy_rankings,
    })
    logger.info("Saved %d candidate signals", len(candidates))
    for c in candidates[:3]:
        logger.info(
            "  %s %s %s conf=%d tree=%s",
            c["symbol"],
            c["side"],
            c["setup_type"],
            c["confidence"],
            c.get("confidence_tree"),
        )
    logger.info("=== Decision Loop complete ===")
    return output


if __name__ == "__main__":
    run()