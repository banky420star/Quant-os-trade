"""Learning self-monitor — is the learning actually HELPING?

`learning_health.py` answers "are trades rich enough to learn from?" (data
quality). This module answers a different, sharper question: **since the bot
started adapting, is it making more money or less?** A self-learning system
that can change its own weights must also be able to notice when a change made
things worse and raise its hand to roll back — otherwise "self-learning" is
just "self-drifting".

Method
======
Split the recent closed-trade history into two windows by time:
  * ``baseline``  — the older window (how it did before the latest adaptation)
  * ``recent``    — the newer window (how it is doing now)
Score both through the reward engine and compare. A material, sustained drop in
risk-adjusted reward on a sufficient sample is flagged as ``degrading`` with a
``rollback_recommended`` verdict. Everything is observe-only: this module never
mutates weights or config — it writes a verdict that the operator (or a gated
loop) acts on.

Honesty
=======
Small samples are reported as ``insufficient`` and never trigger a rollback
recommendation. Trading is stochastic; one bad window is not proof of harm, so
the drop must clear an absolute margin AND a minimum sample in both windows.
"""

from __future__ import annotations

from typing import Any

from core.reward_engine import reward_score
from core.utils import read_json_state, utc_now_iso, write_json_state

# Minimum trades required in BOTH windows before a verdict can be actionable.
_MIN_WINDOW = 20
# How much risk-adjusted reward must fall (recent - baseline, in score units)
# before we call it a real regression rather than variance.
_DEGRADE_SCORE_DROP = 0.5
# Expectancy floor: even a smooth series is "degrading" if it went net-negative.
_NEG_EXPECTANCY_R = -0.05


def _closed_trades() -> list[dict[str, Any]]:
    data = read_json_state("trade_log.json", default=None)
    if not isinstance(data, dict) or not data.get("trades"):
        # trade_log.json is the preferred source (populated in MT5 mode by
        # trade_log_loop). If it's empty, try the MT5 closed-trade ledger before
        # falling back to paper_trades.json (which is empty in MT5 mode).
        mt5 = read_json_state("mt5_trades.json", default=None)
        if isinstance(mt5, dict) and mt5.get("trades"):
            data = mt5
        else:
            data = read_json_state("paper_trades.json", default={"trades": []}) or {}
    trades = list(data.get("trades") or [])
    # Chronological order: prefer closed_at, fall back to list order.
    def _key(t: dict[str, Any]) -> str:
        return str(t.get("closed_at") or t.get("recorded_at") or "")
    if all(t.get("closed_at") for t in trades):
        trades.sort(key=_key)
    return trades


def assess_learning_performance(
    trades: list[dict[str, Any]] | None = None,
    split: float = 0.5,
) -> dict[str, Any]:
    """Compare recent vs baseline realized reward and return a verdict.

    ``split`` is the fraction of history assigned to the (older) baseline
    window; the remainder is the recent window.
    """
    if trades is None:
        trades = _closed_trades()
    n = len(trades)
    cut = int(n * split)
    baseline_trades = trades[:cut]
    recent_trades = trades[cut:]

    baseline = reward_score(baseline_trades)
    recent = reward_score(recent_trades)

    enough = baseline["n"] >= _MIN_WINDOW and recent["n"] >= _MIN_WINDOW
    score_delta = round(recent["score"] - baseline["score"], 4)
    exp_delta = round(recent["expectancy_r"] - baseline["expectancy_r"], 4)

    if not enough:
        verdict = "insufficient"
        rollback = False
        detail = (
            f"need >= {_MIN_WINDOW} trades per window "
            f"(baseline={baseline['n']}, recent={recent['n']})"
        )
    else:
        degrading = (
            score_delta <= -_DEGRADE_SCORE_DROP
            or recent["expectancy_r"] <= _NEG_EXPECTANCY_R
        )
        improving = score_delta >= _DEGRADE_SCORE_DROP and recent["expectancy_r"] > 0
        if degrading:
            verdict = "degrading"
            rollback = True
            detail = (
                f"risk-adjusted reward fell {score_delta:+.2f} "
                f"(recent expectancy {recent['expectancy_r']:+.3f}R) — roll back recent adaptation"
            )
        elif improving:
            verdict = "improving"
            rollback = False
            detail = f"risk-adjusted reward rose {score_delta:+.2f} — keep adapting"
        else:
            verdict = "stable"
            rollback = False
            detail = f"reward change {score_delta:+.2f} within noise band"

    return {
        "timestamp": utc_now_iso(),
        "verdict": verdict,
        "rollback_recommended": rollback,
        "score_delta": score_delta,
        "expectancy_delta_r": exp_delta,
        "baseline": baseline,
        "recent": recent,
        "min_window": _MIN_WINDOW,
        "detail": detail,
    }


def run_learning_monitor(persist: bool = True) -> dict[str, Any]:
    """Assess and (optionally) persist the verdict to state/learning_monitor.json."""
    verdict = assess_learning_performance()
    if persist:
        write_json_state("learning_monitor.json", verdict)
    return verdict
