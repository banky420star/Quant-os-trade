"""Composite trade score — strategy, session, trend, volume, spread, volatility."""

from __future__ import annotations

from typing import Any

from core.session_scorer import resolve_trading_session, session_score

DEFAULT_WEIGHTS: dict[str, float] = {
    "strategy_rank": 0.30,
    "session": 0.20,
    "trend_strength": 0.15,
    "volume": 0.15,
    "spread_quality": 0.10,
    "volatility": 0.10,
}

TREND_STRENGTH_SCORES = {
    "strong": 100,
    "moderate": 66,
    "weak": 33,
}

VOLATILITY_REGIME_SCORES = {
    "high": 85,
    "normal": 70,
    "low": 45,
}


def _strategy_rank_score(rank_info: dict[str, Any]) -> float:
    if not rank_info:
        return 50.0
    if rank_info.get("insufficient_data"):
        return 50.0
    return float(min(100, max(0, rank_info.get("score", rank_info.get("win_rate_pct", 50)))))


def _trend_strength_score(ctx: dict[str, Any]) -> float:
    return float(TREND_STRENGTH_SCORES.get(ctx.get("trend_strength", "weak"), 33))


def _volume_score(feat: dict[str, Any]) -> float:
    ratio = float(feat.get("volume_ratio", 0) or 0)
    return round(min(100, max(0, ratio * 100)), 1)


def _spread_quality_score(
    symbol: str,
    feat: dict[str, Any],
    config: dict[str, Any],
) -> float:
    spread_pts = feat.get("spread_points", feat.get("spread"))
    if spread_pts is None:
        return 75.0

    max_spread = float(config.get("filters", {}).get("max_spread_points", {}).get(symbol, 999))
    if max_spread <= 0:
        return 75.0

    spread_pts = float(spread_pts)
    ratio = spread_pts / max_spread
    if ratio <= 0.25:
        return 100.0
    if ratio <= 0.5:
        return 85.0
    if ratio <= 0.75:
        return 70.0
    if ratio <= 1.0:
        return 55.0
    return max(0.0, round(40 - (ratio - 1.0) * 40, 1))


def _volatility_score(feat: dict[str, Any]) -> float:
    regime = feat.get("volatility_regime", "normal")
    base = float(VOLATILITY_REGIME_SCORES.get(regime, 70))
    atr_ratio = float(feat.get("atr_ratio", 0) or 0)
    min_atr = float(feat.get("min_atr_ratio", 0) or 0)
    if atr_ratio >= min_atr:
        return base
    return max(30.0, base * 0.6)


def min_trade_score_threshold(config: dict[str, Any]) -> float:
    scoring = config.get("session_scoring", {})
    base = float(scoring.get("min_trade_score", 80))
    if config.get("trading", {}).get("aggressive_mode"):
        return float(scoring.get("min_trade_score_aggressive", max(55, base - 15)))
    return base


def compute_trade_score(
    symbol: str,
    feat: dict[str, Any],
    ctx: dict[str, Any],
    rank_info: dict[str, Any],
    config: dict[str, Any],
    *,
    session: str | None = None,
) -> dict[str, Any]:
    """Weighted 0-100 trade score with per-component breakdown."""
    scoring = config.get("session_scoring", {})
    if not scoring.get("enabled", True):
        return {
            "total": 100.0,
            "passed": True,
            "threshold": min_trade_score_threshold(config),
            "enabled": False,
            "components": {},
        }

    weights = {**DEFAULT_WEIGHTS, **scoring.get("weights", {})}
    weight_sum = sum(weights.values()) or 1.0
    weights = {k: v / weight_sum for k, v in weights.items()}

    session_name = session or ctx.get("session") or resolve_trading_session()
    sess = session_score(session_name, symbol, config)

    components = {
        "strategy_rank": _strategy_rank_score(rank_info),
        "session": sess["normalized"],
        "trend_strength": _trend_strength_score(ctx),
        "volume": _volume_score(feat),
        "spread_quality": _spread_quality_score(symbol, feat, config),
        "volatility": _volatility_score(feat),
    }

    total = round(sum(components[k] * weights[k] for k in weights if k in components), 1)
    threshold = min_trade_score_threshold(config)
    passed = total >= threshold

    return {
        "total": total,
        "passed": passed,
        "threshold": threshold,
        "enabled": True,
        "session_detail": sess,
        "weights": weights,
        "components": {k: round(v, 1) for k, v in components.items()},
        "weighted": {
            k: round(components[k] * weights[k], 2) for k in weights if k in components
        },
    }