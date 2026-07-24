"""Self-learning background loop — reward, self-monitoring, experiments.

Runs three observe-only steps every cycle and writes their state for the
dashboard:

  1. reward_weighted_weights  — candidate subsystem weights from realized-profit
     credit assignment (never auto-deployed; replay/operator-gated).
  2. learning_monitor         — is recent risk-adjusted reward better or worse
     than baseline? Flags rollback when the bot's own adaptation hurt results.
  3. shadow_experiments       — refresh bounded candidate arms and score any
     that have accumulated forward paper trades.

Everything here is OFF the money path. It proposes and reports; it never places
an order or writes live config. Enable/disable via config: self_learning.enabled
(default True) — disabling it costs the bot nothing but the ability to improve.
"""

from __future__ import annotations

import logging

from core.adaptive_weights import AdaptiveWeightOptimizer, load_weights
from core.learning_monitor import run_learning_monitor
from core.shadow_experiment import run_shadow_experiments
from core.utils import load_config, write_json_state

_logger = logging.getLogger("self_learning_loop")


def run(config: dict | None = None) -> dict:
    config = config or load_config()
    cfg = config.get("self_learning", {}) if isinstance(config, dict) else {}
    if not cfg.get("enabled", True):
        return {"self_learning_loop": "disabled"}

    out: dict = {}

    # 1) Reward-weighted candidate weights (from realized profit).
    try:
        opt = AdaptiveWeightOptimizer(config, _logger)
        candidate = opt.reward_weighted_weights(
            min_trades=int(cfg.get("min_trades", 15))
        )
        write_json_state("reward_weight_candidate.json", candidate)
        out["reward_weights"] = candidate.get("status")
    except Exception as exc:  # observe-only: never let learning break the bot
        _logger.warning("reward_weighted_weights failed: %s", exc)
        out["reward_weights"] = f"error:{exc}"

    # 2) Learning self-monitor (rollback recommendation on degradation).
    try:
        verdict = run_learning_monitor(persist=True)
        out["learning_monitor"] = verdict.get("verdict")
        if verdict.get("rollback_recommended"):
            _logger.warning(
                "Learning monitor: %s — %s", verdict.get("verdict"), verdict.get("detail")
            )
    except Exception as exc:
        _logger.warning("learning_monitor failed: %s", exc)
        out["learning_monitor"] = f"error:{exc}"

    # 3) Shadow experiments (bounded, proposal-only).
    try:
        deployed = load_weights(config)
        ledger = run_shadow_experiments(
            config=config, deployed_weights=deployed, persist=True
        )
        out["shadow_experiments"] = ledger.get("promoted_count", 0)
    except Exception as exc:
        _logger.warning("shadow_experiments failed: %s", exc)
        out["shadow_experiments"] = f"error:{exc}"

    return {"self_learning_loop": "OK", **out}
