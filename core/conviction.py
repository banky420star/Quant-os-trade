"""Conviction engine — grade every setup like a professional trader.

A retail bot takes every signal at the same size. A professional does two
things a bot usually doesn't:

  1. **Grades the setup.** Not "is this tradeable?" (yes/no) but "how good is
     this, really?" — A+, A, B, or C — from the *confluence* of independent
     reasons to take it. One reason is a hunch; five aligned reasons is a
     conviction trade.
  2. **Sizes to conviction and passes on the marginal.** Full size on the A's,
     a probe on the B's, and — the hardest discipline — *nothing* on the C's.
     Patience is the edge most retail traders never develop.

This module turns the evidence the pipeline already computes (trend alignment,
regime fit, reward:risk, session quality, historical edge of this exact setup,
volume/structure confirmation, and the decision engine's confidence) into a
single grade, a size multiplier, and a one-line thesis that reads like a
trader's note.

Honesty
=======
A grade is a disciplined weighting of *current evidence*, not a prediction. An
A+ setup can still lose — grading improves the *average* quality and size
discipline of the trades taken, which is what compounds. Size multipliers are
<= 1.0 by default: conviction only ever *reduces* risk below the configured
baseline unless the operator explicitly opts into upsizing the A+ tier.
"""

from __future__ import annotations

from typing import Any

from core.setup_library import is_regime_compatible

# Confluence factor weights (sum ~1.0). Each factor scores 0..100.
_WEIGHTS: dict[str, float] = {
    "confidence": 0.20,        # decision-engine subsystem consensus
    "trend_alignment": 0.18,   # M5 + M15 agree with the trade direction
    "reward_risk": 0.18,       # asymmetry — pros demand it
    "historical_edge": 0.16,   # this exact setup's realised win rate (lean into what works)
    "regime_fit": 0.12,        # setup matches the market regime
    "session_quality": 0.10,   # trading the symbol's liquid/preferred session
    "confirmation": 0.06,      # volume + structure (rejection/breakout) backing
}

# Grade thresholds on the 0..100 conviction score.
_GRADE_BANDS = [
    ("A+", 82.0),
    ("A", 70.0),
    ("B", 56.0),
    ("C", 0.0),
]

# Default size multiplier per grade. <= 1.0 so conviction only trims risk on
# weaker setups; the operator can raise A+ above 1.0 to upsize best ideas.
_DEFAULT_SIZE_BY_GRADE: dict[str, float] = {
    "A+": 1.0,
    "A": 1.0,
    "B": 0.6,
    "C": 0.3,
}

# Order for "min grade to trade" comparisons (higher index = stronger).
_GRADE_ORDER = ["C", "B", "A", "A+"]


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def reward_risk_ratio(signal: dict[str, Any]) -> float | None:
    """Realised R:R from entry/sl/tp1. None when levels are missing."""
    entry = _safe_float(signal.get("entry"))
    sl = _safe_float(signal.get("sl"))
    tp1 = _safe_float(signal.get("tp1"))
    if not entry or not sl or not tp1:
        return None
    risk = abs(entry - sl)
    reward = abs(tp1 - entry)
    if risk <= 0:
        return None
    return reward / risk


def _score_reward_risk(rr: float | None) -> float:
    if rr is None:
        return 50.0
    # 1:1 is mediocre (40), 1.5 solid (65), 2 strong (85), 3+ excellent (100).
    if rr >= 3.0:
        return 100.0
    if rr >= 2.0:
        return 85.0
    if rr >= 1.5:
        return 68.0
    if rr >= 1.0:
        return 45.0
    return max(0.0, rr * 30.0)


def _score_trend_alignment(signal: dict[str, Any], feat: dict[str, Any]) -> float:
    side = str(signal.get("side", "")).upper()
    m5 = str(feat.get("m5_trend", "")).lower()
    m15 = str(feat.get("m15_trend", "")).lower()
    want = "bullish" if side == "BUY" else "bearish"
    agree = (1 if m5 == want else (-1 if m5 in ("bullish", "bearish") else 0)) + \
            (1 if m15 == want else (-1 if m15 in ("bullish", "bearish") else 0))
    # both agree -> 100, one agrees / one neutral -> 75, mixed -> 50, both against -> 10
    return {2: 100.0, 1: 75.0, 0: 50.0, -1: 30.0, -2: 10.0}[agree]


def _score_historical_edge(stats: dict[str, Any]) -> float:
    """Win rate of this exact setup, discounted when the sample is thin."""
    if not stats:
        return 50.0
    total = int(stats.get("total", 0) or 0)
    wr = _safe_float(stats.get("win_rate_pct", 50.0), 50.0)
    if total < 5:
        return 50.0  # not enough evidence — treat as neutral
    # Confidence in the sample scales 5..40 trades from 0..1.
    sample_conf = min(1.0, (total - 5) / 35.0)
    # Pull win rate toward 50 when the sample is thin.
    return _clamp(50.0 + (wr - 50.0) * sample_conf)


def _score_regime_fit(signal: dict[str, Any]) -> float:
    setup = signal.get("setup_type", "")
    mc = signal.get("market_context") or {}
    reg = (mc.get("market_regime") or {}) if isinstance(mc, dict) else {}
    primary = reg.get("primary") or mc.get("regime") or ""
    if not primary:
        return 60.0  # unknown regime — mild neutral
    return 100.0 if is_regime_compatible(setup, primary) else 20.0


def _score_session_quality(signal: dict[str, Any]) -> float:
    ts = signal.get("trade_score") or {}
    sess = ts.get("session_detail") or {}
    if "normalized" in sess:
        return _clamp(_safe_float(sess.get("normalized"), 60.0))
    return 60.0


def _score_confirmation(feat: dict[str, Any]) -> float:
    vol = _safe_float(feat.get("volume_ratio", 0))
    score = _clamp(vol * 60.0)  # 1.0x volume -> 60, 1.6x -> ~96
    rej = str(feat.get("rejection", "")).lower()
    brk = str(feat.get("breakout", "")).lower()
    if "rejection" in rej and rej != "none":
        score = min(100.0, score + 15.0)
    if brk and brk != "none":
        score = min(100.0, score + 10.0)
    return score


def grade_signal(
    signal: dict[str, Any],
    feat: dict[str, Any],
    config: dict[str, Any] | None = None,
    setup_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Grade a candidate signal on professional confluence.

    Returns a dict with:
        grade         "A+"|"A"|"B"|"C"
        conviction    0..100 weighted confluence score
        size_mult     risk multiplier for this grade (<= 1.0 by default)
        take          bool — is this at/above the configured min grade to trade
        thesis        one-line trader's rationale
        factors       per-factor 0..100 breakdown
    """
    config = config or {}
    conv_cfg = config.get("conviction", {}) if isinstance(config, dict) else {}
    size_by_grade = {**_DEFAULT_SIZE_BY_GRADE, **(conv_cfg.get("size_by_grade") or {})}
    min_grade = str(conv_cfg.get("min_grade_to_trade", "C"))

    rr = reward_risk_ratio(signal)
    factors = {
        "confidence": _clamp(_safe_float(signal.get("confidence"), 50.0)),
        "trend_alignment": _score_trend_alignment(signal, feat),
        "reward_risk": _score_reward_risk(rr),
        "historical_edge": _score_historical_edge(setup_stats or {}),
        "regime_fit": _score_regime_fit(signal),
        "session_quality": _score_session_quality(signal),
        "confirmation": _score_confirmation(feat),
    }
    conviction = round(sum(factors[k] * _WEIGHTS[k] for k in _WEIGHTS), 1)

    grade = "C"
    for name, floor in _GRADE_BANDS:
        if conviction >= floor:
            grade = name
            break

    size_mult = float(size_by_grade.get(grade, 1.0))
    take = _GRADE_ORDER.index(grade) >= _GRADE_ORDER.index(min_grade) if min_grade in _GRADE_ORDER else True

    return {
        "grade": grade,
        "conviction": conviction,
        "size_mult": round(size_mult, 3),
        "take": bool(take),
        "thesis": _build_thesis(signal, grade, conviction, rr, factors),
        "factors": {k: round(v, 1) for k, v in factors.items()},
        "reward_risk": round(rr, 2) if rr is not None else None,
    }


def _build_thesis(
    signal: dict[str, Any],
    grade: str,
    conviction: float,
    rr: float | None,
    factors: dict[str, float],
) -> str:
    """A one-line note in a trader's voice, leading with the strongest reasons."""
    sym = signal.get("symbol", "?")
    side = str(signal.get("side", "")).upper()
    setup = str(signal.get("setup_type", "setup")).replace("_", " ")

    reasons: list[str] = []
    if factors["trend_alignment"] >= 90:
        reasons.append("full multi-timeframe trend alignment")
    elif factors["trend_alignment"] <= 30:
        reasons.append("trading against the higher timeframe")
    if rr is not None:
        reasons.append(f"{rr:.1f}:1 reward:risk")
    if factors["historical_edge"] >= 65:
        reasons.append("a proven historical edge here")
    elif factors["historical_edge"] <= 35:
        reasons.append("a weak track record for this setup")
    if factors["regime_fit"] >= 90:
        reasons.append("regime-aligned")
    elif factors["regime_fit"] <= 25:
        reasons.append("fighting the regime")
    if factors["session_quality"] >= 75:
        reasons.append("prime session")

    lead = {
        "A+": "A+ conviction",
        "A": "A-grade",
        "B": "B-grade probe",
        "C": "C-grade — pass/minimal",
    }[grade]
    body = "; ".join(reasons[:3]) if reasons else "mixed evidence"
    return f"{lead} {side} {sym} {setup} ({conviction:.0f}/100): {body}."


def stronger_or_equal(grade: str, floor: str) -> bool:
    """True if ``grade`` is at least ``floor`` in the A+ > A > B > C order."""
    if grade not in _GRADE_ORDER or floor not in _GRADE_ORDER:
        return True
    return _GRADE_ORDER.index(grade) >= _GRADE_ORDER.index(floor)
