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
  and free; actually routing paper trades through an executable gate arm is a
  separate, explicitly-opted-in step the operator controls. Weight arms remain
  proposal-only until the decision layer applies their patches.
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

# Routing (2026-08-04). Shadow experiment arms only accumulate forward trades
# when the execution layer stamps each executed signal with the arm it belongs
# to. This is OFF by default — the module stays a proposal-only scorer until an
# operator explicitly opts in via self_learning.shadow_experiments.routing_enabled.
# Routing is paper-mode ONLY (never MT5): a signal is deterministically bucketed
# (stable by signal_id) into control or one arm, gate arms raise the confidence /
# R:R floor so the arm's trade set genuinely differs from control, and the arm id
# is stamped onto the signal so it survives into the closed trade in
# paper_trades.json where score_arms() reads it.
ROUTING_CONFIG_KEY = "shadow_experiments"
DEFAULT_ROUTING_ENABLED = False
# Fraction of signals reserved for the control sample (remainder = arms).
# Clamped at runtime so a misconfig cannot eliminate the control arm.
ROUTING_CONTROL_FRACTION = 0.5
# Only families whose behavior is applied by the current execution path may be
# routed. Weight patches are proposals for a future decision-layer adapter; they
# must not be stamped as if the approved signal had actually used the patch.
ROUTABLE_EXPERIMENT_FAMILIES = frozenset({"gate"})


def routing_config(config: dict[str, Any]) -> dict[str, Any]:
    """Resolved shadow-experiment routing config (all opt-in, default off)."""
    sl = config.get("self_learning") or {}
    exp = sl.get(ROUTING_CONFIG_KEY) or {}
    if not isinstance(exp, dict):
        exp = {}
    return {
        "routing_enabled": bool(exp.get("routing_enabled", DEFAULT_ROUTING_ENABLED)),
        "control_fraction": float(exp.get("control_fraction", ROUTING_CONTROL_FRACTION)),
    }


def _stable_bucket(signal_id: str, n_buckets: int) -> int:
    """Stable, repeatable bucket for a signal_id across cycles."""
    return int(hashlib.sha1(str(signal_id).encode("utf-8")).hexdigest()[:8], 16) % n_buckets


def annotate_experiment_arm_signals(
    approved: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Stamp each paper signal with its experiment arm (or leave as control).

    Paper-mode only — MT5 signals are returned unchanged so this can never
    alter live execution. When routing is disabled, returns ``approved``
    untouched (the default, so this is a safe no-op).

    Deterministic by signal_id: the same signal is always routed to the same
    arm, and repeated cycles do not re-bucket an already-stamped signal.
    Gate arms (confidence / R:R floor) filter marginal candidates out of the
    arm's trade set so the arm genuinely trades a different population than
    control. Weight arms remain proposal-only because the current execution
    layer does not apply their weight patch.
    """
    config = config or {}
    if str((config.get("execution") or {}).get("mode") or "paper").lower() != "paper":
        return approved
    rcfg = routing_config(config)
    if not rcfg["routing_enabled"]:
        return approved

    ledger = read_json_state(LEDGER_FILE, default={}) or {}
    experiments = list(ledger.get("experiments") or [])
    if not experiments:
        # First run: propose the arm set deterministically so stamping and the
        # scorer agree on arm ids. Persisted so future runs reuse it.
        experiments = generate_experiments(config=config)
        ledger["experiments"] = experiments
        write_json_state(LEDGER_FILE, ledger)
    # Weight arms are intentionally proposal-only until the decision layer can
    # apply their patch before signals are approved. Routing them here would
    # create a label without changing behavior and yield a false experiment.
    experiments = [
        exp for exp in experiments
        if exp.get("family") in ROUTABLE_EXPERIMENT_FAMILIES
    ]
    if not experiments:
        return approved

    out: list[dict[str, Any]] = []
    n_arms = len(experiments)
    control_fraction = min(0.9, max(0.1, float(rcfg["control_fraction"])))
    bucket_count = 10_000
    control_cutoff = int(bucket_count * control_fraction)
    arm_bucket_count = bucket_count - control_cutoff

    for record in approved:
        signal = record.get("signal", record) if isinstance(record, dict) else {}
        if not isinstance(signal, dict) or not signal.get("signal_id"):
            out.append(record)
            continue
        sid = str(signal["signal_id"])
        # Deterministic control/arm split. The first control_fraction of the
        # stable bucket space is control; the remainder is evenly divided among
        # arms. This keeps a real control sample while making the configured
        # fraction meaningful, and remains stable across process restarts.
        bucket = _stable_bucket(sid, bucket_count)
        if bucket < control_cutoff:
            out.append(record)  # control — untouched (no experiment_arm stamp)
            continue
        arm_index = min(
            n_arms - 1,
            ((bucket - control_cutoff) * n_arms) // arm_bucket_count,
        )
        arm = experiments[arm_index]
        candidate = dict(signal)
        candidate["experiment_arm"] = arm["id"]
        # Gate arms: enforce the arm's raised floor so the arm's trade set
        # differs from control. A signal rejected by the arm is dropped from
        # this experiment route; it must not be reclassified as control because
        # that would bias the control sample with trades the arm would skip.
        if arm.get("family") == "gate":
            patch = arm.get("patch") or {}
            min_conf = float(patch.get("signals.min_confidence") or 0)
            min_rr = float(patch.get("signals.min_risk_reward") or 0)
            conf = float(signal.get("confidence") or 0)
            rr = float(signal.get("risk_reward") or signal.get("min_risk_reward") or 0)
            if min_conf and conf < min_conf:
                continue
            if min_rr and rr < min_rr:
                continue
        if "signal" in record:
            out.append({**record, "signal": candidate})
        else:
            out.append(candidate)
    return out

# Promotion gates.
MIN_ARM_TRADES = 25          # arm must have this many forward trades to judge
MIN_CONTROL_TRADES = 25      # require a comparable control sample as well
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
    control_n = control.get("n", 0)
    margin = round(arm.get("score", 0.0) - control.get("score", 0.0), 4)
    arm_exp = arm.get("expectancy_r", 0.0)

    if arm_n < MIN_ARM_TRADES:
        status = "running"
        detail = f"arm has {arm_n}/{MIN_ARM_TRADES} trades — keep collecting"
    elif control_n < MIN_CONTROL_TRADES:
        status = "running"
        detail = f"control has {control_n}/{MIN_CONTROL_TRADES} trades — keep collecting"
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
        if exp.get("family") not in ROUTABLE_EXPERIMENT_FAMILIES:
            verdict = {
                "status": "running",
                "reward_margin": 0.0,
                "detail": "proposal-only arm is not routed until its patch is executable",
            }
        else:
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
            "min_control_trades": MIN_CONTROL_TRADES,
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
