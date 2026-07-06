"""Evaluation loop — corrective entry + management policy before verifier."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.evaluation_policy import evaluate_batch, evaluation_enabled, evaluation_mode
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
        return doc

    features = read_json_state("features.json", default={})
    trades = read_json_state("paper_trades.json", default={"trades": []})
    spread_data: dict[str, float] = {}
    for sym, feat in (features.get("symbols") or {}).items():
        sp = feat.get("spread_points", feat.get("spread"))
        if sp is not None:
            spread_data[sym] = float(sp)

    evaluated, skipped = evaluate_batch(
        candidates,
        features,
        config,
        spread_data=spread_data,
        recent_trades=list(trades.get("trades") or []),
        logger=logger,
    )

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