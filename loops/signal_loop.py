"""Agent 3: Decision Loop — evidence-first candidate signals via Decision Engine.

Extended with strategy diversification (2026-07-22):
  - Five parallel signal streams:
    1. DecisionEngine (existing)
    2. Bankbot MA crossover (existing)
    3. Donchian breakout (NEW — trend)
    4. Bollinger reversion (NEW — FX-only, contrarian)
    5. ATR-expansion breakout (NEW — vol-trigger)
  - Each stream gets a fraction of bot risk budget (risk parity).
  - Meta-decision picks the best per symbol per cycle (confidence x regime fit x bias).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.atr_expansion_signal import generate_atr_expansion_signals
from core.bankbot_signal import bankbot_enabled, generate_bankbot_signals
from core.bollinger_signal import generate_bollinger_signals
from core.decision_engine import DecisionEngine
from core.donchian_signal import generate_donchian_signals
from core.entry_pipeline import refine_candidates
from core.market_regime import MarketRegimeEngine
from core.setup_triggers import write_setup_catalog
from core.state_store import sync_store_from_doc
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
from strategies.portfolio import (
    annotate_candidate_for_risk_parity,
    meta_decide,
)


# Per-symbol cooldowns keyed by (symbol, source) to prevent re-fire across cycles.
COOLDOWN_TTL_SECONDS = 3600  # entries older than 1h are pruned each run()
_donchian_cooldown: dict[str, dict[str, object]] = {}
_bollinger_cooldown: dict[str, dict[str, object]] = {}
_atr_expansion_cooldown: dict[str, dict[str, object]] = {}


def _prune_cooldowns(states: list[dict[str, dict[str, object]]]) -> int:
    """Drop cooldown entries older than COOLDOWN_TTL_SECONDS. Returns total pruned count."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    pruned = 0
    for state in states:
        for sym in list(state.keys()):
            ts_str = state[sym].get("timestamp")
            try:
                if isinstance(ts_str, str):
                    if ts_str.endswith("Z"):
                        ts = _dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    else:
                        ts = _dt.datetime.fromisoformat(ts_str).replace(tzinfo=_dt.timezone.utc)
                elif isinstance(ts_str, _dt.datetime) and ts_str.tzinfo is None:
                    ts = ts_str.replace(tzinfo=_dt.timezone.utc)
                else:
                    ts = ts_str
                if (now - ts).total_seconds() > COOLDOWN_TTL_SECONDS:
                    del state[sym]
                    pruned += 1
            except (TypeError, ValueError):
                # Drop unparseable entries immediately on prune pass.
                try:
                    del state[sym]
                    pruned += 1
                except KeyError:
                    pass
    return pruned


def _diversification_enabled(config) -> bool:
    div = (config.get("strategies") or {}).get("diversification") or {}
    return bool(div.get("enabled", False))


def run() -> dict | None:
    """Create evidence-based candidate signals — no verification or execution."""
    config = load_config()
    logger = setup_logger("signal_loop", "signal_loop.log")
    logger.info("=== Decision Loop starting (evidence-first) ===")

    if fail_safe_missing("features.json", logger):
        return None
    if fail_safe_missing("market_context.json", logger):
        return None

    # Hygiene: prune stale cooldown entries so the three module-level dicts do
    # not grow unbounded across multi-day bot runs.
    _prune_cooldowns([
        _donchian_cooldown, _bollinger_cooldown, _atr_expansion_cooldown,
    ])

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

    mode = str((config.get("execution") or {}).get("mode") or "paper").lower()
    positions_file = "mt5_positions.json" if mode == "mt5" else "paper_positions.json"
    positions_data = read_json_state(positions_file, default={"positions": []})
    open_positions = list(positions_data.get("positions") or [])

    engine = DecisionEngine(config, logger)
    ranker_obj = StrategyRanker(config, logger)

    # ------------------------------------------------------------------
    # 1. DecisionEngine primary stream
    # ------------------------------------------------------------------
    raw_candidates: list[dict] = []
    try:
        dec = engine.generate_candidates(features, context_data, edge_scores)
        for c in dec:
            annotate_candidate_for_risk_parity(c, config)
            c.setdefault("source", "decision_engine")
        raw_candidates.extend(dec)
    except Exception as exc:
        logger.warning("DecisionEngine stream failed: %s", exc)

    # ------------------------------------------------------------------
    # 2. Bankbot MA crossover (existing secondary stream)
    # ------------------------------------------------------------------
    if bankbot_enabled(config):
        try:
            bb = generate_bankbot_signals(config, logger=logger)
            for c in bb:
                annotate_candidate_for_risk_parity(c, config)
                c.setdefault("source", "bankbot")
            if bb:
                logger.info("Bankbot added %d candidate signals", len(bb))
            raw_candidates.extend(bb)
        except Exception as bbexc:
            logger.warning("Bankbot signal generation failed: %s", bbexc)

    # ------------------------------------------------------------------
    # 3-5. Diversification streams (Donchian / Bollinger / ATR-Expansion)
    # ------------------------------------------------------------------
    if _diversification_enabled(config):
        try:
            dn = generate_donchian_signals(
                config, logger=logger, _cooldown_state=_donchian_cooldown,
            )
            if dn:
                logger.info("Donchian added %d candidate signals", len(dn))
            raw_candidates.extend(dn)
        except Exception as exc:
            logger.warning("Donchian signal generation failed: %s", exc)

        try:
            bl = generate_bollinger_signals(
                config, logger=logger, _cooldown_state=_bollinger_cooldown,
            )
            if bl:
                logger.info("Bollinger added %d candidate signals", len(bl))
            raw_candidates.extend(bl)
        except Exception as exc:
            logger.warning("Bollinger signal generation failed: %s", exc)

        try:
            ax = generate_atr_expansion_signals(
                config, logger=logger, _cooldown_state=_atr_expansion_cooldown,
            )
            if ax:
                logger.info("ATR-expansion added %d candidate signals", len(ax))
            raw_candidates.extend(ax)
        except Exception as exc:
            logger.warning("ATR-expansion signal generation failed: %s", exc)

    # ------------------------------------------------------------------
    # Meta-decision (only when diversification enabled)
    # ------------------------------------------------------------------
    if _diversification_enabled(config):
        try:
            regime_engine = MarketRegimeEngine(logger)
            regime_data = regime_engine.classify_all(features, context_data)
            regime_ctx = regime_data.get("symbols", {})
        except Exception as exc:
            logger.warning("Regime classification failed: %s", exc)
            regime_ctx = {}

        div = (config.get("strategies") or {}).get("diversification") or {}
        per_symbol_cap = int(div.get("per_symbol_cap", 1))
        min_meta_score = float(div.get("min_meta_score", 0.20))

        chosen, diag = meta_decide(
            raw_candidates, regime_ctx,
            min_score=min_meta_score, per_symbol_cap=per_symbol_cap,
        )
        logger.info(
            "Meta-decision: %d considered -> %d chosen (%s)",
            diag.get("considered", len(raw_candidates)),
            diag.get("chosen_count", len(chosen)),
            diag.get("by_stream"),
        )
        candidates_input = chosen
    else:
        diag = {"considered": len(raw_candidates), "chosen_count": len(raw_candidates),
                "by_stream": {}, "chosen": []}
        candidates_input = raw_candidates

    candidates = refine_candidates(
        candidates_input,
        features,
        context_data,
        config,
        positions=open_positions,
        logger=logger,
    )
    arena_report = record_triggers(candidates, config, logger=logger) if arena_enabled(config) else {}

    strategy_rankings: dict[str, list] = {}
    for symbol, feat in features.get("symbols", {}).items():
        try:
            ctx = context_data.get("symbols", {}).get(symbol, {})
            strategy_rankings[symbol] = ranker_obj.rank_for_symbol(symbol, ctx, feat)
        except Exception as exc:
            logger.warning("Rank failed for %s: %s", symbol, exc)

    top_explain = candidates[0].get("explain") if candidates else None
    output = {
        "timestamp": utc_now_iso(),
        "engine": "decision_engine+diversification",
        "diversification_enabled": _diversification_enabled(config),
        "count": len(candidates),
        "raw_candidate_count": len(raw_candidates),
        "considered_by_stream": diag.get("by_stream", {}),
        "meta_chosen": diag.get("chosen", []),
        "candidates": candidates,
        "top_explain": top_explain,
        "strategy_rankings": strategy_rankings,
        "strategy_arena": leaderboard() if arena_enabled(config) else None,
        "arena_triggers_recorded": arena_report.get("recorded", 0),
    }
    write_json_state("candidate_signals.json", output)
    sync_store_from_doc(config, "signals", output)
    write_json_state("strategy_rankings.json", {
        "timestamp": utc_now_iso(),
        "rankings": strategy_rankings,
    })
    write_json_state("meta_decision_diagnostics.json", diag)
    logger.info("Saved %d candidate signals", len(candidates))
    for c in candidates[:3]:
        logger.info(
            "  %s %s %s conf=%d score=%s source=%s",
            c["symbol"],
            c["side"],
            c["setup_type"],
            c["confidence"],
            c.get("meta_score"),
            c.get("source"),
        )
    logger.info("=== Decision Loop complete ===")
    return output


if __name__ == "__main__":
    run()