"""Named strategy parameter overlays for the out-of-sample evaluator.

Each variant is a frozen set of parameter overrides applied on top of the
profitability-first base config (``load_config()`` already syncs the
``performance`` section into ``signals`` / ``risk`` / ``trading`` /
``execution``). These are hand-picked sensible presets — NOT the output of any
in-window optimisation. Sweeping parameters to maximise a single window's PnL
is exactly the overfitting the legacy benchmark committed; we deliberately do
not do that here.

Only knobs that already exist in the config are varied, so every variant runs
through the same live pipeline (FeatureEngine → DecisionEngine → Verifier →
PaperBroker) with no special-case code paths.
"""

from __future__ import annotations

import copy
from typing import Any

# Dotted path -> value. Nested dicts are addressed with "/"; intermediate
# sections are created on demand so variants can introduce a knob the base
# config doesn't set.
VariantOverlay = dict[str, Any]

STRATEGY_VARIANTS: dict[str, VariantOverlay] = {
    "baseline": {
        "description": "Current shipped config on the real $80 capital — the control. "
                       "Live record (713 trades, 43.2% wr @1.15 RR) implies ~-0.07R/trade.",
        # No parameter overrides — base config as-is.
    },
    "strict_trend": {
        "description": "Higher confidence floor, wider reward floor, stricter session score. "
                       "Fewer, higher-conviction trades; favours clean trend-aligned setups.",
        "signals/min_confidence": 70,
        "signals/min_risk_reward": 1.5,
        "session_scoring/min_trade_score": 70,
        "session_scoring/min_trade_score_aggressive": 70,
        "performance/min_confidence": 70,
        "performance/min_risk_reward": 1.5,
        "performance/min_trade_score": 70,
    },
    "conservative": {
        "description": "Smaller risk per trade, wider reward demand, fewer session trades, "
                       "trades off sooner. Designed to minimise drawdown, accept lower frequency.",
        "signals/default_risk_percent": 0.25,
        "signals/min_risk_reward": 1.6,
        "signals/min_confidence": 68,
        "session_scoring/min_trade_score": 66,
        "trading/max_session_trades_per_symbol": 6,
        "trading/trailing/activation_atr_mult": 0.6,
        "trading/trailing/trail_atr_mult": 0.25,
        "performance/default_risk_percent": 0.25,
        "performance/min_risk_reward": 1.6,
        "performance/min_confidence": 68,
        "performance/min_trade_score": 66,
    },
    "scalper": {
        "description": "Lower reward floor, lower score floor, tighter trailing — more trades, "
                       "smaller wins. Tests whether high frequency beats selectivity here.",
        "signals/min_risk_reward": 1.0,
        "signals/min_confidence": 60,
        "signals/default_risk_percent": 0.3,
        "session_scoring/min_trade_score": 52,
        "session_scoring/min_trade_score_aggressive": 52,
        "trading/max_session_trades_per_symbol": 20,
        "trading/trailing/activation_atr_mult": 0.5,
        "trading/trailing/trail_atr_mult": 0.2,
        "performance/min_risk_reward": 1.0,
        "performance/min_confidence": 60,
        "performance/min_trade_score": 52,
        "performance/default_risk_percent": 0.3,
    },
    "wide_swing": {
        "description": "Larger reward floor, wider trailing — fewer trades that let winners run. "
                       "Tests the opposite end of the selectivity spectrum vs scalper.",
        "signals/min_risk_reward": 2.0,
        "signals/min_confidence": 70,
        "signals/default_risk_percent": 0.5,
        "session_scoring/min_trade_score": 72,
        "trading/max_session_trades_per_symbol": 8,
        "trading/trailing/activation_atr_mult": 1.0,
        "trading/trailing/trail_atr_mult": 0.5,
        "trading/break_even/trigger_atr_mult": 0.75,
        "performance/min_risk_reward": 2.0,
        "performance/min_confidence": 70,
        "performance/min_trade_score": 72,
        "performance/default_risk_percent": 0.5,
    },
    "bocpd_gate": {
        "description": "PRE-REGISTERED causal change-point GATE (Adams-MacKay 2007 BOCPD, "
                       "hazard lambda=430 bars) layered on the baseline config. NOT a tuned "
                       "strategy swap: a single pre-registered filter that drops candidate "
                       "signals on bars flagged as changepoint/transition. Counts as ~1 trial "
                       "for DSR (no in-data hyperparameter tuning). Defaults: cp_max_run=1, "
                       "transition_max_run=5, warmup=200 bars. See core/bocpd_gate.py.",
        # Enable the BOCPD gate knob on top of the baseline overlay. All other
        # knobs are inherited from the base config (identical to ``baseline``
        # when this knob is unset).
        "regime_gating/bocpd_enabled": True,
        "regime_gating/skip_changepoint_bars": True,
        "regime_gating/hazard_lambda": 430.0,
        "regime_gating/cp_max_run": 1,
        "regime_gating/transition_max_run": 5,
        "regime_gating/warmup_bars": 200,
    },
    "session_gate": {
        "description": "PRE-REGISTERED session gate: drop candidate signals whose bar UTC "
                       "hour is outside the London/NY overlap (12:00-16:00 UTC) -- the "
                       "tightest-spread XAU window per core/session_scorer.py "
                       "(overlap_london_ny, 'best for XAU'). The window is an EXISTING "
                       "structural definition in the codebase, not a parameter fit on this "
                       "data, so this adds ~0 new DSR trials. Tests the COST-side lever the "
                       "regime research identified: does concentrating trades in the "
                       "cheapest-spread session lift net-R above zero at retail 30 bps, "
                       "where the gross +0.35R edge otherwise inverts to negative? Honest "
                       "prior: ~12% to clear DSR 0.95.",
        "regime_gating/session_gate_enabled": True,
        "regime_gating/session_start_utc": 12,
        "regime_gating/session_end_utc": 16,
    },
}

# Order for deterministic reporting.
VARIANT_ORDER = ["baseline", "strict_trend", "conservative", "scalper", "wide_swing", "bocpd_gate", "session_gate"]


def apply_overlay(base: dict[str, Any], overlay: VariantOverlay) -> dict[str, Any]:
    """Return a deepcopy of ``base`` with dotted-path overrides applied.

    A ``"description"`` key (or any non-path key) in the overlay is carried
    through under ``variant_meta`` rather than written into the config.
    """
    cfg = copy.deepcopy(base)
    for key, value in overlay.items():
        if "/" not in key:
            continue  # skip meta keys like "description"
        section = cfg
        parts = key.split("/")
        for part in parts[:-1]:
            section = section.setdefault(part, {})
        section[parts[-1]] = value
    return cfg


def build_variant_config(base: dict[str, Any], name: str, *, capital_usd: float) -> dict[str, Any]:
    """Build the config used to evaluate ``name`` at the given real capital base."""
    overlay = STRATEGY_VARIANTS.get(name)
    if overlay is None:
        raise KeyError(f"unknown strategy variant: {name!r} (have {list(STRATEGY_VARIANTS)})")
    cfg = apply_overlay(base, overlay)

    # Evaluate on the REAL capital base, not the fictional $1M projection base.
    cfg["execution"]["starting_cash"] = float(capital_usd)
    cfg["execution"]["mode"] = "paper"
    cfg["execution"]["live_trading_enabled"] = False
    cfg["execution"]["mt5_trading_enabled"] = False
    # NEVER let evaluator replays write into the live edge_database.json — that
    # contaminates the live edge DB with paper trades (and is what made the live
    # edge DB's setup_type field look mis-populated). Replays are read-only on
    # the edge DB. The win-condition learner captures context from returned
    # trades instead, so it does not need edge-db ingestion either.
    cfg.setdefault("quant", {})["replay_ingest_edge_db"] = False
    return cfg


def variant_description(name: str) -> str:
    return str(STRATEGY_VARIANTS.get(name, {}).get("description", ""))