"""Evaluation loop — corrective entry + management policy before verifier."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.evaluation_policy import evaluate_batch, evaluation_enabled, evaluation_mode
from core.trade_history import read_closed_trades
from core.fast_mode import fast_mode_enabled
from core.fast_signal_cache import refresh_cache
from core.state_store import (
    candidates_available,
    read_candidate_signals,
    sync_store_from_doc,
)
from core.utils import (
    fail_safe_missing,
    load_config,
    read_json_state,
    setup_logger,
    utc_now_iso,
    write_json_state,
)


def run() -> dict | None:
    config = load_config()
    logger = setup_logger("evaluation_loop", "evaluation_loop.log")

    if not evaluation_enabled(config):
        logger.info("Evaluation loop disabled — passthrough")
        return None

    # Adaptive gate throttle (USER-AUTHORIZED 2026-07-08): tighten entry gates
    # when the bot is losing, relax when winning, and auto-pause any symbol on
    # a losing streak. Computed every cycle and surfaced to state for the UI.
    from core.adaptive_gates import compute_adaptive_gates
    ag = compute_adaptive_gates(config)
    if ag.get("enabled"):
        config = dict(config)
        _ev = dict(config.get("evaluation") or {})
        if ag.get("min_policy_score") is not None:
            _ev["min_policy_score"] = ag["min_policy_score"]
        _bl = list(_ev.get("symbol_blocklist") or [])
        for _sym in (ag.get("blocked_symbols") or []):
            if _sym not in _bl:
                _bl.append(_sym)
        _ev["symbol_blocklist"] = _bl
        config["evaluation"] = _ev
        logger.info(
            "Adaptive gates: tier=%s min_score=%.1f blocked=%s (%s)",
            ag.get("tier"), ag.get("min_policy_score"), ag.get("blocked_symbols"), ag.get("reason"),
        )

    if not candidates_available(config) and fail_safe_missing("candidate_signals.json", logger):
        return None
    if fail_safe_missing("features.json", logger):
        return None

    candidates_data = read_candidate_signals(config) or {}
    candidates = list(candidates_data.get("candidates") or [])
    if not candidates:
        logger.info("No candidates to evaluate")
        doc = {
            "timestamp": utc_now_iso(),
            "mode": evaluation_mode(config),
            "count": 0,
            "evaluated": [],
            "skipped": [],
            "source": "evaluation_loop",
        }
        write_json_state("evaluated_signals.json", doc)
        sync_store_from_doc(config, "evaluated", doc)
        if fast_mode_enabled(config):
            refresh_cache(config, evaluated_doc=doc, logger=logger)
        return doc

    features = read_json_state("features.json", default={})
    # Keep live MT5 evaluation isolated from the paper/research ledger. The
    # selected history feeds recent-symbol and policy gates, so mixing ledgers
    # can silently block valid live entries with stale paper losses.
    trades = read_closed_trades(config)
    spread_data: dict[str, float] = {}
    for sym, feat in (features.get("symbols") or {}).items():
        try:
            sp = feat.get("spread_points", feat.get("spread"))
            if sp is not None:
                spread_data[sym] = float(sp)
        except Exception as exc:
            logger.warning("spread read failed for %s: %s", sym, exc)

    evaluated, skipped = evaluate_batch(
        candidates,
        features,
        config,
        spread_data=spread_data,
        recent_trades=trades,
        logger=logger,
    )

    # Paper data-lab only: retain policy-filtered opportunities for later
    # forward labeling. This is observational and never changes evaluated
    # signals or the MT5 path.
    try:
        from core.forward_opportunity_labeler import record_filtered_signals
        record_filtered_signals(skipped, config, source="evaluation", now=utc_now_iso())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Forward opportunity labeling skipped: %s", exc)

    doc = {
        "timestamp": utc_now_iso(),
        "mode": evaluation_mode(config),
        "count": len(evaluated),
        "skipped_count": len(skipped),
        "evaluated": evaluated,
        "skipped": skipped,
        "source": "evaluation_loop",
        "candidate_count": len(candidates),
    }
    write_json_state("evaluated_signals.json", doc)
    sync_store_from_doc(config, "evaluated", doc)
    if fast_mode_enabled(config):
        refresh_cache(config, evaluated_doc=doc, logger=logger)
    logger.info(
        "Evaluated %d candidates → %d executable, %d skipped (mode=%s)",
        len(candidates),
        len(evaluated),
        len(skipped),
        evaluation_mode(config),
    )
    return doc


if __name__ == "__main__":
    run()