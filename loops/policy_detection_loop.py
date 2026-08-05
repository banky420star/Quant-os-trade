"""Policy detection loop — rank execution-policy variants in shadow mode."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.policy_detection import detect_policies, policy_detection_enabled, policy_detection_mode
from core.state_store import read_evaluated_signals, sync_store_from_doc
from core.trade_history import trade_history_filename
from core.utils import (
    load_config,
    read_json_state,
    setup_logger,
    write_json_state,
)


def run() -> dict | None:
    config = load_config()
    logger = setup_logger("policy_detection_loop", "policy_detection_loop.log")

    if not policy_detection_enabled(config):
        logger.info("Policy detection disabled — skip")
        return None

    features = read_json_state("features.json", default=None)
    if not isinstance(features, dict):
        logger.error("Required input missing or invalid: features.json")
        return None

    trades = read_json_state(trade_history_filename(config), default={"trades": []})
    # JSON is the hot-loop mirror and follows the current utils.STATE_DIR
    # binding (including isolated workers). Prefer it before the optional
    # SQLite read path, whose store may have been initialized in another
    # process/state root; SQLite remains the fallback for deployments that
    # disable the JSON mirror.
    evaluated_doc = read_json_state("evaluated_signals.json", default=None)
    if not isinstance(evaluated_doc, dict):
        evaluated_doc = read_evaluated_signals(config) or {"evaluated": []}
    doc = detect_policies(trades, evaluated_doc, features, config)
    write_json_state("policy_scores.json", doc)
    sync_store_from_doc(config, "policy_scores", doc)

    logger.info(
        "Policy detection scored %d variants across %d cells (mode=%s)",
        doc.get("variant_count", 0),
        doc.get("cell_count", 0),
        policy_detection_mode(config),
    )
    return doc


if __name__ == "__main__":
    run()