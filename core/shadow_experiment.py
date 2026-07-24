"""Shadow experiment manager — try new things in the background, safely.

The bot proposes small, bounded, reversible variations of its own tunables
(subsystem weights, gate thresholds), runs them as *shadow arms*, and scores
each arm on its OWN forward outcomes through the reward engine. An arm is only
ever **proposed** for promotion when it beats the control arm by a real margin
on a sufficient sample. Nothing here mutates live config or places orders.

Why it is built this way
========================
"Let the bot try new things" is how trading bots blow up accounts — a random
parameter change on live money is just gambling with extra steps. Three hard
rules make experimentation safe:

  1. **Bounded.** Every arm is a small, capped perturbation of a known-good
     baseline (see MAX_WEIGHT_SHIFT / gate steps). No arm can propose an
     extreme config.
  2. **Shadow / forward-evaluated.** An arm is judged by the reward engine on
     the trades taken *under that arm*, not by an in-sample curve fit. This is
     honest out-of-sample evidence, not overfitting.
  3. **Promotion-gated, proposal-only.** Beating control needs a minimum
     sample AND a reward margin. Even then the module only writes a proposal;
     applying it is a separate, explicit operator/gated action.

What this module does NOT do
============================
* It does not enable live shadow execution by itself. Registering arms is safe
  and free; actually routing paper/live trades through an arm is a separate,
  explicitly-opted-in step the operator controls.
* It never guarantees an arm will keep winning. Promotion means "beat control
  out-of-sample on this sample" — evidence, not a promise.
"""

from __future__ import annotations

import hashlib
from typing import Any

from core.reward_engine import reward_score
from core.utils import read_json_state, utc_now_iso, write_json_state
from core.weight_defaults import SUBSYSTEM_WEIGHTS

LEDGER_FILE = "shadow_experiments.json"

# Bounds — an arm can never move a weight more than this fraction of baseline.
MAX_WEIGHT_SHIFT = 0.15
# Promotion gates.
MIN_ARM_TRADES = 25          # arm must have this many forward trades to judge
MIN_REWARD_MARGIN = 0.30     # arm score must beat control by this (score units)
MIN_EXPECTANCY_R = 0.0       # arm must be net-profitable in R, not just > control


def _arm_id(name: str, patch: dict[str, Any]) -> str:
    h = hashlib.sha1(f"{name}:{sorted(patch.items())}".encode()).hexdigest()[:8]
    return f"{name}_{h}"


def generate_experiments(
    deployed_weights: dict[str, float] | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Propose a bounded set of candidate arms around the current baseline.

    Two families, both small and reversible:
      * weight arms   — nudge one subsystem engine up by MAX_WEIGHT_SHIFT
      * gate arms     — nudge min_confidence / min_risk_reward by a small step
    """
    base_w = dict(deployed_weights or SUBSYSTEM_WEIGHTS)
    config = config or {}
    arms: list[dict[str, Any]] = []

    for engine in base_w:
        patch = {f"weights.{engine}": round(base_w[engine] * (1 + MAX_WEIGHT_SHIFT), 4)}
        arms.append({
            "id": _arm_id(f"w_{engine}", patch),
            "family": "weight",
            "hypothesis": f"up-weighting {engine} by {int(MAX_WEIGHT_SHIFT*100)}% improves risk-adjusted reward",
            "patch": patch,
        })

    signals = config.get("signals", {}) if isinstance(config, dict) else {}
    base_conf = float(signals.get("min_confidence", 40))
    base_rr = float(signals.get("min_risk_reward", 1.0))
    gate_arms = [
        ("gate_conf_up", {"signals.min_confidence": round(base_conf + 5, 2)},
         "a stricter confidence gate lifts expectancy by cutting marginal trades"),
        ("gate_rr_up", {"signals.min_risk_reward": round(base_rr + 0.15, 3)},
         "a higher min R:R lifts expectancy by dropping poor-payoff setups"),
    ]
    for name, patch, hyp in gate_arms:
        arms.append({
            "id": _arm_id(name, patch),
            "family": "gate",
            "hypothesis": hyp,
            "patch": patch,
        })
    return arms


def promotion_verdict(
    control: dict[str, Any],
    arm: dict[str, Any],
) -> dict[str, Any]:
    """Decide whether an arm has earned a promotion proposal vs control.

    Requires: enough arm trades, a reward margin over control, and net-positive
    arm expectancy. All three, or it stays 'running' / 'reject'.
    """
    arm_n = arm.get("n", 0)
    margin = round(arm.get("score", 0.0) - control.get("score", 0.0), 4)
    arm_exp = arm.get("expectancy_r", 0.0)

    if arm_n < MIN_ARM_TRADES:
        status = "running"
        detail = f"arm has {arm_n}/{MIN_ARM_TRADES} trades — keep collecting"
    elif margin >= MIN_REWARD_MARGIN and arm_exp > MIN_EXPECTANCY_R:
        status = "promote"
        detail = f"beats control by {margin:+.2f} at {arm_exp:+.3f}R expectancy — propose for deploy"
    else:
        status = "reject"
        detail = f"margin {margin:+.2f} / expectancy {arm_exp:+.3f}R below promotion bar"
    return {"status": status, "reward_margin": margin, "detail": detail}


def score_arms(
    trades: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Score each arm on trades tagged with its id; control = untagged trades.

    Trades are expected to carry an ``experiment_arm`` field (stamped by the
    execution layer when shadow routing is enabled). Until routing is enabled
    every arm has 0 trades and stays 'running' — which is the correct, honest
    state, not an error.
    """
    control_trades = [t for t in trades if not t.get("experiment_arm")]
    control = reward_score(control_trades)
    results: list[dict[str, Any]] = []
    for exp in experiments:
        arm_trades = [t for t in trades if t.get("experiment_arm") == exp["id"]]
        arm_reward = reward_score(arm_trades)
        verdict = promotion_verdict(control, arm_reward)
        results.append({
            **exp,
            "reward": arm_reward,
            "verdict": verdict["status"],
            "reward_margin": verdict["reward_margin"],
            "detail": verdict["detail"],
        })
    return results


def run_shadow_experiments(
    config: dict[str, Any] | None = None,
    deployed_weights: dict[str, float] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Refresh the arm set, score arms from paper trades, persist the ledger.

    Returns the full ledger: control reward, every arm with its verdict, and
    any arms currently proposed for promotion.
    """
    config = config or {}
    ledger = read_json_state(LEDGER_FILE, default={}) or {}
    experiments = ledger.get("experiments")
    if not experiments:
        experiments = generate_experiments(deployed_weights, config)

    paper = read_json_state("paper_trades.json", default={"trades": []}) or {}
    trades = list(paper.get("trades") or [])
    control_trades = [t for t in trades if not t.get("experiment_arm")]

    scored = score_arms(trades, experiments)
    promoted = [a for a in scored if a["verdict"] == "promote"]

    out = {
        "timestamp": utc_now_iso(),
        "control_reward": reward_score(control_trades),
        "experiments": scored,
        "promoted": promoted,
        "promoted_count": len(promoted),
        "bounds": {
            "max_weight_shift": MAX_WEIGHT_SHIFT,
            "min_arm_trades": MIN_ARM_TRADES,
            "min_reward_margin": MIN_REWARD_MARGIN,
        },
        "note": (
            "Shadow arms are proposals only. Promotion means an arm beat control "
            "out-of-sample on this sample — evidence, not a guarantee. Applying a "
            "promoted arm is a separate operator/gated action."
        ),
    }
    if persist:
        write_json_state(LEDGER_FILE, out)
    return out
