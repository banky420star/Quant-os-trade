"""Setup Library — explicit setup definitions with rules and regime compatibility."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class SetupDefinition:
    name: str
    display_name: str
    allowed_regimes: tuple[str, ...]
    blocked_regimes: tuple[str, ...] = ()
    min_confidence: float = 0.5
    min_rr: float = 1.2
    description: str = ""
    entry_hints: list[str] = field(default_factory=list)
    exit_hints: list[str] = field(default_factory=list)


SETUP_LIBRARY: dict[str, SetupDefinition] = {
    "trend_continuation": SetupDefinition(
        name="trend_continuation",
        display_name="Trend Continuation",
        allowed_regimes=("strong_trend", "weak_trend", "expansion", "compression", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.55,
        description="Trade with aligned trend after momentum confirmation.",
        entry_hints=["M5 and M15 aligned", "move_type continuation", "volume confirming"],
        exit_hints=["SL beyond structure", "TP at 1.5R minimum"],
    ),
    "pullback": SetupDefinition(
        name="pullback",
        display_name="Pullback",
        allowed_regimes=("strong_trend", "weak_trend", "expansion", "compression", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.5,
        description="Retest of broken level or EMA in trending market.",
        entry_hints=["Pullback to structure", "Trend bias intact"],
        exit_hints=["SL beyond pullback swing", "TP at prior high/low"],
    ),
    "breakout": SetupDefinition(
        name="breakout",
        display_name="Breakout",
        allowed_regimes=("expansion", "compression", "strong_trend", "accumulation"),
        blocked_regimes=("range", "distribution"),
        min_confidence=0.55,
        description="Break of key support/resistance with volume.",
        entry_hints=["Breakout candle closed", "Volume above average"],
        exit_hints=["SL inside range", "TP measured move"],
    ),
    "compression_breakout": SetupDefinition(
        name="compression_breakout",
        display_name="Compression Breakout",
        allowed_regimes=("compression", "accumulation"),
        blocked_regimes=("strong_trend", "volatility_spike"),
        min_confidence=0.55,
        description="Volatility squeeze release.",
        entry_hints=["BB squeeze", "ATR compression resolving"],
        exit_hints=["SL inside coil", "TP 2x coil width"],
    ),
    "range_fade": SetupDefinition(
        name="range_fade",
        display_name="Range Fade",
        allowed_regimes=("range", "distribution", "accumulation"),
        blocked_regimes=("strong_trend", "weak_trend", "expansion"),
        min_confidence=0.5,
        description="Fade extremes in defined range.",
        entry_hints=["Price at range boundary", "No trend alignment"],
        exit_hints=["SL beyond range", "TP mid-range"],
    ),
    "liquidity_sweep": SetupDefinition(
        name="liquidity_sweep",
        display_name="Liquidity Sweep",
        allowed_regimes=("range", "weak_trend", "distribution", "accumulation"),
        blocked_regimes=("volatility_spike",),
        min_confidence=0.55,
        description="Stop hunt beyond level followed by rejection.",
        entry_hints=["Wick beyond S/R", "Rejection candle", "Liquidity score high"],
        exit_hints=["SL beyond sweep wick", "TP opposite range bound"],
    ),
    "mean_reversion": SetupDefinition(
        name="mean_reversion",
        display_name="Mean Reversion",
        allowed_regimes=("range", "distribution", "accumulation", "compression"),
        blocked_regimes=("strong_trend", "expansion"),
        min_confidence=0.5,
        description="Fade extension from Bollinger bands.",
        entry_hints=["BB position extreme", "Not in strong trend"],
        exit_hints=["SL beyond band", "TP middle band"],
    ),
    "false_breakout": SetupDefinition(
        name="false_breakout",
        display_name="False Breakout",
        allowed_regimes=("range", "distribution", "accumulation", "weak_trend"),
        blocked_regimes=("strong_trend", "expansion"),
        min_confidence=0.5,
        description="Failed breakout with rejection back into range.",
        entry_hints=["Breakout failure", "Rejection at extreme"],
        exit_hints=["SL beyond false break", "TP range mid"],
    ),
}


def get_setup(name: str) -> SetupDefinition | None:
    return SETUP_LIBRARY.get(name)


def is_regime_compatible(setup_type: str, primary_regime: str) -> bool:
    defn = get_setup(setup_type)
    if not defn:
        return True
    if primary_regime in defn.blocked_regimes:
        return False
    if defn.allowed_regimes and primary_regime not in defn.allowed_regimes:
        return False
    return True


def enrich_setup_stats(
    setup_type: str,
    edge_scores: dict[str, Any],
    symbol: str | None = None,
) -> dict[str, Any]:
    """Attach memory stats to a setup definition."""
    defn = get_setup(setup_type)
    stats_src = edge_scores.get("setup_stats", {})
    if symbol and symbol in stats_src.get("by_symbol", {}):
        s = stats_src["by_symbol"][symbol].get(setup_type, {})
    else:
        s = stats_src.get("global", {}).get(setup_type, {})
    return {
        "name": setup_type,
        "display_name": defn.display_name if defn else setup_type,
        "description": defn.description if defn else "",
        "win_rate_pct": s.get("win_rate_pct", 0),
        "total": s.get("total", 0),
        "wins": s.get("wins", 0),
        "losses": s.get("losses", 0),
        "entry_hints": defn.entry_hints if defn else [],
        "exit_hints": defn.exit_hints if defn else [],
    }


def list_setups() -> list[dict[str, Any]]:
    return [
        {
            "name": d.name,
            "display_name": d.display_name,
            "allowed_regimes": list(d.allowed_regimes),
            "blocked_regimes": list(d.blocked_regimes),
            "min_confidence": d.min_confidence,
        }
        for d in SETUP_LIBRARY.values()
    ]