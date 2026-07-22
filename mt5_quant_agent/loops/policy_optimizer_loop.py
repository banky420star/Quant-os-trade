"""Policy optimizer loop — sample-gated best policy selection per symbol/setup/session."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.policy_optimizer import policy_optimizer_enabled, run as optimize_policies
from core.utils import load_config, setup_logger


def run() -> dict | None:
    config = load_config()
    logger = setup_logger("policy_optimizer_loop", "policy_optimizer_loop.log")

    if not policy_optimizer_enabled(config):
        logger.info("Policy optimizer disabled")
        return None

    logger.info("=== Policy optimizer loop starting ===")
    doc = optimize_policies(config, logger=logger)
    logger.info("=== Policy optimizer loop complete ===")
    return doc


if __name__ == "__main__":
    run()