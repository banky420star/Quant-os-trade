"""Profitability reward engine — the single source of truth for "was this good?".

Every self-learning component (weight optimizer, learning monitor, shadow
experiments) scores outcomes through THIS module, so "better" always means the
same thing: more risk-adjusted realized profit, not more wins, not higher
confidence.

Design principles
=================
* **Realized R is the unit.** A trade's reward is its R-multiple (profit in
  units of the risk taken). A +3R win counts three times a +1R win; a -1R loss
  counts three times a -0.3R loss. Win/loss *counts* throw this information
  away — expectancy does not.
* **Risk-adjusted, not raw.** Two strategies with the same mean R are not
  equal if one gets there through violent swings. The headline score is a
  Sortino-style ratio (expectancy over downside deviation) so a smoother
  equity path scores higher. Raw expectancy is reported alongside.
* **Honest about small samples.** Every score carries its sample size. Callers
  gate on `n` before acting; this module never hides thin evidence.

What this module does NOT do
============================
* It does not predict future PnL or guarantee profit. It scores *past*
  outcomes. Past reward is evidence, not a promise.
* It does not place trades, mutate config, or deploy anything.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from core.weight_defaults import SUBSYSTEM_WEIGHTS

# A trade must clear this |R| to be treated as a real outcome rather than
# rounding noise around break-even.
_R_EPS = 1e-6


def trade_r_multiple(trade: dict[str, Any]) -> float | None:
    """Best-effort realized R-multiple for a closed trade.

    Prefers the explicit ``r_multiple`` stamped by the trade tracker. Falls
    back to a coarse proxy from ``pnl`` sign / ``result`` so trades that closed
    before R was computed still contribute a direction (never a magnitude we
    can't justify).
    """
    r = trade.get("r_multiple")
    if r is not None:
        try:
            return float(r)
        except (TypeError, ValueError):
            pass
    result = str(trade.get("result") or "").lower()
    pnl = trade.get("pnl")
    sign = 0.0
    if result == "win" or (pnl is not None and _safe_float(pnl) > 0):
        sign = 1.0
    elif result == "loss" or (pnl is not None and _safe_float(pnl) < 0):
        sign = -1.0
    if sign == 0.0:
        return None
    # Edge-DB records carry ``rr`` as an unsigned realized magnitude (the sign
    # lives in ``result``/``pnl``). Reconstruct signed R so credit assignment
    # gets the true size, not just a ±1 direction.
    rr = trade.get("rr")
    if rr is not None:
        mag = abs(_safe_float(rr))
        if mag > _R_EPS:
            return sign * mag
    # No magnitude available: contribute direction only, never an invented size.
    return sign


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def r_multiples(trades: Iterable[dict[str, Any]]) -> list[float]:
    """Extract the resolvable R-multiples from a trade iterable."""
    out: list[float] = []
    for t in trades:
        r = trade_r_multiple(t)
        if r is not None:
            out.append(r)
    return out


def expectancy(rs: list[float]) -> float:
    """Mean R per trade — the raw edge. Positive means net-profitable."""
    return sum(rs) / len(rs) if rs else 0.0


def downside_deviation(rs: list[float], target: float = 0.0) -> float:
    """Root-mean-square of shortfalls below ``target`` (Sortino denominator).

    Only losses relative to target are penalized — upside volatility is not a
    risk. Returns 0.0 when nothing fell below target.
    """
    if not rs:
        return 0.0
    shortfalls = [min(0.0, r - target) for r in rs]
    msq = sum(s * s for s in shortfalls) / len(rs)
    return math.sqrt(msq)


def profit_factor(rs: list[float]) -> float:
    """Gross win R divided by gross loss R. >1 is profitable; inf if no losers."""
    gains = sum(r for r in rs if r > _R_EPS)
    losses = -sum(r for r in rs if r < -_R_EPS)
    if losses <= _R_EPS:
        return math.inf if gains > _R_EPS else 0.0
    return gains / losses


def win_rate(rs: list[float]) -> float:
    """Fraction of trades with positive R (0..1)."""
    if not rs:
        return 0.0
    return sum(1 for r in rs if r > _R_EPS) / len(rs)


def reward_score(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Score a set of closed trades by risk-adjusted realized profitability.

    The headline ``score`` is a Sortino-style ratio: expectancy divided by
    downside deviation. It rewards positive expectancy achieved with a smooth
    (low-downside) equity path and penalizes edges bought with large drawdowns.

    Interpretation:
        score > 0   net-profitable, risk-adjusted
        score ~ 0   break-even / no edge
        score < 0   net-losing

    Always inspect ``n`` before acting: a high score on 6 trades is noise.
    """
    trades = list(trades)
    rs = r_multiples(trades)
    n = len(rs)
    exp = expectancy(rs)
    dd = downside_deviation(rs)
    # Sortino-style risk adjustment. When there is no downside at all (every
    # trade >= 0), fall back to raw expectancy scaled up — a genuinely
    # loss-free sample should score strongly, not divide by zero.
    if dd > _R_EPS:
        score = exp / dd
    else:
        score = exp * 2.0 if exp > 0 else 0.0
    total_r = sum(rs)
    return {
        "n": n,
        "score": round(score, 4),
        "expectancy_r": round(exp, 4),
        "downside_deviation_r": round(dd, 4),
        "profit_factor": (round(profit_factor(rs), 4) if math.isfinite(profit_factor(rs)) else None),
        "win_rate": round(win_rate(rs), 4),
        "total_r": round(total_r, 4),
        "sufficient": n >= 30,
    }


def engine_credit(
    trades: Iterable[dict[str, Any]],
    engines: list[str] | None = None,
    neutral: float = 50.0,
) -> dict[str, float]:
    """Reward-weighted credit assignment per decision engine.

    For each engine, credit = sum over trades of
    ``(engine_confidence - neutral) * realized_R``.

    An engine that voted *above neutral* on trades that realized *positive R*
    accrues positive credit; voting confidently on losers accrues negative
    credit. Unlike win/loss counting, a +3R winner moves the credit three times
    as much as a +1R winner — the reward is proportional to money made.

    Returns raw (un-normalized) credit per engine; callers turn credit into
    weights (see AdaptiveWeightOptimizer.reward_weighted_weights).
    """
    engines = engines or list(SUBSYSTEM_WEIGHTS.keys())
    credit = {e: 0.0 for e in engines}
    for t in trades:
        r = trade_r_multiple(t)
        if r is None:
            continue
        tree = t.get("confidence_tree") or (t.get("signal_meta") or {}).get("confidence_tree") or {}
        if not isinstance(tree, dict) or not tree:
            continue
        for e in engines:
            conf = _safe_float(tree.get(e, neutral), neutral)
            credit[e] += (conf - neutral) * r
    return {e: round(c, 4) for e, c in credit.items()}


def credit_to_weights(
    credit: dict[str, float],
    baseline: dict[str, float] | None = None,
    max_shift: float = 0.5,
) -> dict[str, float]:
    """Convert per-engine credit into a normalized weight vector.

    Positive-credit engines are up-weighted, negative-credit engines
    down-weighted, but each engine's multiplier is bounded to
    ``[1 - max_shift, 1 + max_shift]`` of its baseline so a noisy sample can
    never zero out or explode a single engine. The result is renormalized to
    sum to 1.0.
    """
    baseline = dict(baseline or SUBSYSTEM_WEIGHTS)
    engines = list(baseline.keys())
    total_base = sum(baseline.values()) or 1.0
    norm_base = {e: baseline[e] / total_base for e in engines}

    cvals = [credit.get(e, 0.0) for e in engines]
    cmax = max((abs(c) for c in cvals), default=0.0)
    if cmax <= _R_EPS:
        return {e: round(norm_base[e], 4) for e in engines}

    adjusted: dict[str, float] = {}
    for e in engines:
        # Scale credit into [-max_shift, +max_shift] relative to the strongest
        # signal in this batch, then apply as a bounded multiplier.
        rel = credit.get(e, 0.0) / cmax
        mult = 1.0 + max(-max_shift, min(max_shift, rel * max_shift))
        adjusted[e] = norm_base[e] * mult

    total = sum(adjusted.values()) or 1.0
    return {e: round(adjusted[e] / total, 4) for e in engines}
