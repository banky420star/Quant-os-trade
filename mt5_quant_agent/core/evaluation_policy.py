"""Corrective execution policy — attaches entry + management recipe to candidates."""

from __future__ import annotations

import logging
from typing import Any

from core.policy_score import (
    compute_policy_score,
    recent_symbol_stats,
    session_entry_bias,
)


def evaluation_enabled(config: dict[str, Any]) -> bool:
    return bool((config.get("evaluation") or {}).get("enabled", True))


def evaluation_mode(config: dict[str, Any]) -> str:
    return str((config.get("evaluation") or {}).get("mode") or "shadow")


def _eval_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("evaluation") or {}


def _pick_entry_type(
    signal: dict[str, Any],
    feat: dict[str, Any],
    session_bias: dict[str, Any],
    policy_score: float,
) -> str:
    if signal.get("within_reach") is True and float(signal.get("distance_atr") or 99) <= 0.35:
        vol = float(feat.get("volume_ratio") or feat.get("relative_volume") or 1.0)
        if vol >= 1.0 and session_bias.get("entry_type") == "market":
            return "market"
    if float(signal.get("distance_atr") or 0) > 0.75:
        return "limit"
    return str(session_bias.get("entry_type") or signal.get("entry_mode") or "limit")


def _build_management_profile(session_bias: dict[str, Any], entry_type: str) -> dict[str, Any]:
    """Legacy builder — prefer scenario_management_recipe for new paths."""
    return {
        "entry_type": entry_type,
        "limit_offset_atr": float(session_bias.get("limit_offset_atr") or 0.1),
        "sl_model": "structure_atr",
        "sl_atr_mult": float(session_bias.get("sl_atr_mult") or 1.15),
        "tp_model": "rr",
        # Wider defaults: tight BE/trail was the live payoff killer (WR high, $ net neg).
        "tp1_r": float(session_bias.get("tp1_r") or 1.2),
        "tp2_r": float(session_bias.get("tp2_r") or 1.7),
        "break_even_enabled": True,
        "break_even_trigger_r": float(session_bias.get("break_even_trigger_r") or 0.75),
        "break_even_lock_r": float(session_bias.get("break_even_lock_r") or 0.2),
        "trailing_enabled": True,
        "trail_start_r": float(session_bias.get("trail_start_r") or 1.0),
        "trail_atr_mult": float(session_bias.get("trail_atr_mult") or 0.5),
        "max_hold_minutes": int(session_bias.get("max_hold_minutes") or 35),
        "cancel_if_not_filled_seconds": int(session_bias.get("cancel_if_not_filled_seconds") or 110),
    }


def merge_best_policy(
    session_bias: dict[str, Any],
    *,
    symbol: str,
    setup: str,
    session: str,
) -> dict[str, Any]:
    """Bias session_entry_bias from best_policies when gate is suggest+ and samples suffice."""
    try:
        from core.policy_optimizer import policy_cell_key
        from core.utils import read_json_state
    except ImportError:
        return session_bias

    doc = read_json_state("best_policies.json", default={}) or {}
    policies = doc.get("policies") or {}
    if not isinstance(policies, dict):
        return session_bias

    cell = policy_cell_key(symbol, setup, session)
    row = policies.get(cell)
    if not isinstance(row, dict):
        return session_bias

    gate = str(row.get("gate") or "observe")
    if gate not in ("suggest", "soft", "auto"):
        return session_bias
    sample_n = int(row.get("sample_n") or 0)
    if sample_n < 10:
        return session_bias

    merged = dict(session_bias)
    mgmt = row.get("management_profile") or {}
    if isinstance(mgmt, dict):
        for key in (
            "entry_type",
            "limit_offset_atr",
            "sl_atr_mult",
            "tp1_r",
            "tp2_r",
            "break_even_trigger_r",
            "trail_start_r",
            "trail_atr_mult",
            "max_hold_minutes",
            "cancel_if_not_filled_seconds",
        ):
            if key in mgmt and mgmt[key] is not None:
                merged[key] = mgmt[key]
    if row.get("entry_type"):
        merged["entry_type"] = row["entry_type"]
    merged["best_policy_gate"] = gate
    merged["best_policy_score"] = row.get("policy_score")
    return merged


def _global_edge_stats(symbol: str, setup: str) -> dict[str, Any] | None:
    """Long-term win rate for a symbol+setup from edge_scores.json (all-time)."""
    try:
        from core.utils import read_json_state
    except ImportError:
        return None
    doc = read_json_state("edge_scores.json", default={}) or {}
    by_symbol = (doc.get("setup_stats") or {}).get("by_symbol") or {}
    cell = (by_symbol.get(symbol) or {}).get(setup)
    if isinstance(cell, dict):
        return cell
    return None


def evaluate_candidate(
    signal: dict[str, Any],
    feat: dict[str, Any],
    config: dict[str, Any],
    *,
    spread_points: float | None = None,
    recent_trades: list[dict[str, Any]] | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Return candidate enriched with execution_policy and management_profile."""
    log = logger or logging.getLogger("evaluation_policy")
    cfg = _eval_cfg(config)
    symbol = signal.get("symbol", "")
    session = (signal.get("market_context") or {}).get("session") if isinstance(signal.get("market_context"), dict) else None
    setup = str(signal.get("setup_type") or "unknown")
    session_bias = session_entry_bias(str(session or "unknown"))
    session_bias = merge_best_policy(
        session_bias,
        symbol=str(symbol),
        setup=setup,
        session=str(session or "unknown"),
    )
    recent = recent_symbol_stats(list(recent_trades or []), symbol)

    score, reason_parts = compute_policy_score(
        signal,
        feat,
        spread_points=spread_points,
        recent=recent,
        session_bias=session_bias,
    )

    # Regime-aware score bias: preferred setups in this market get a small lift;
    # demoted / untradeable regimes tighten or skip.
    regime_eff: dict[str, Any] = {}
    try:
        from core.regime_evolution import (
            effective_regime_settings,
            regime_evolution_enabled,
        )

        if regime_evolution_enabled(config):
            mc = signal.get("market_context") if isinstance(signal.get("market_context"), dict) else {}
            mr = mc.get("market_regime") if isinstance(mc.get("market_regime"), dict) else {}
            regime_label = mr.get("primary") or mc.get("regime")
            regime_eff = effective_regime_settings(
                config,
                symbol=str(symbol),
                regime=regime_label,
                setup=setup,
            )
            if regime_eff.get("setup_preferred"):
                score += 4.0
                reason_parts.append(f"regime_preferred:{regime_eff.get('regime')}")
            if regime_eff.get("setup_demoted"):
                score -= 8.0
                reason_parts.append(f"regime_demoted:{regime_eff.get('regime')}")
            if regime_eff.get("skip"):
                score -= 25.0
                reason_parts.append(f"regime_skip:{regime_eff.get('regime')}")
    except Exception:
        regime_eff = {}

    skip_below = float(cfg.get("skip_below_score") or 25)
    min_score = float(cfg.get("min_policy_score") or 35)
    action = "execute"
    if score < skip_below:
        action = "skip"
    elif score < min_score:
        action = "skip"

    if regime_eff.get("skip") and action != "skip":
        action = "skip"
        if f"regime_skip:{regime_eff.get('regime')}" not in reason_parts:
            reason_parts.append(f"regime_skip:{regime_eff.get('regime')}")

    # --- Profit-protection gates (USER-AUTHORIZED 2026-07-08) ---
    # These block negative-edge trades the score alone was too lenient to catch.

    # (1) Explicit symbol blocklist (e.g. a proven structural loser like UK100m
    #     at 1.4% historical win rate). Works under any profile without touching
    #     the scanned symbol universe.
    blocklist = list(cfg.get("symbol_blocklist") or [])
    if str(symbol) in blocklist:
        action = "skip"
        reason_parts.append(f"symbol_blocklist:{symbol}")

    setup_blocklist = list(cfg.get("setup_blocklist") or [])
    if setup in setup_blocklist:
        action = "skip"
        reason_parts.append(f"setup_blocklist:{setup}")

    # (2) Recent cold-streak circuit breaker: a symbol that lost its last N
    #     closes (win_rate < cold_wr) is skipped regardless of entry quality.
    #     Uses the last 20 closed trades already computed as `recent`.
    cold_wr = float(cfg.get("recent_cold_skip_win_rate") or 20.0)
    cold_n = int(cfg.get("recent_cold_skip_min_n") or 8)
    if int(recent.get("n", 0)) >= cold_n and float(recent.get("win_rate_pct", 50.0)) < cold_wr:
        action = "skip"
        reason_parts.append(
            f"recent_cold_symbol_{recent.get('win_rate_pct'):.0f}%/{recent.get('n')}"
        )

    # (3) Global edge cold: when the in-session recent sample is still thin,
    #     consult the all-time symbol+setup win rate so a known-bad cell is
    #     blocked from trade #1 instead of needing 8 in-session losses first.
    if int(recent.get("n", 0)) < cold_n:
        edge = _global_edge_stats(str(symbol), setup)
        if edge:
            g_n = int(edge.get("total", 0))
            g_wr = float(edge.get("win_rate_pct", 50.0))
            g_min_n = int(cfg.get("global_edge_cold_min_n") or 6)
            g_wr_thresh = float(cfg.get("global_edge_cold_win_rate") or 20.0)
            if g_n >= g_min_n and g_wr < g_wr_thresh:
                action = "skip"
                reason_parts.append(f"global_edge_cold_{setup}_{g_wr:.0f}%/{g_n}")

    entry_type = _pick_entry_type(signal, feat, session_bias, score)
    if action != "skip" and entry_type == "limit" and signal.get("within_reach") is False:
        action = "skip"
        reason_parts.append("limit_unreachable")

    # USER 2026-07-15: gold quality bypass only — never override structural skips
    # (blocklist / cold / global-edge). Review bug: prior code forced execute even
    # when XAU or setup was on an operator blocklist.
    try:
        from core.gold_policy import gold_force_pass

        if gold_force_pass(str(symbol), config) and action == "skip":
            structural = any(
                r.startswith("symbol_blocklist:")
                or r.startswith("setup_blocklist:")
                or r.startswith("recent_cold_symbol_")
                or r.startswith("global_edge_cold_")
                for r in reason_parts
            )
            if not structural:
                action = "execute"
                reason_parts.append("gold_never_rejectable_force_execute")
                if signal.get("within_reach") is False:
                    entry_type = "market"
    except Exception:
        pass

    # Scenario-fit recipe: which settings fit *this symbol + regime + session now*
    # (not a global "best strategy"). Ghost promotions override when trusted.
    try:
        from core.trade_manager import (
            scenario_management_recipe,
            snapshot_indicators,
        )

        mgmt = scenario_management_recipe(signal, config, session_bias=session_bias)
        mgmt["entry_type"] = entry_type
        indicator_params = snapshot_indicators(feat)
    except Exception:
        mgmt = _build_management_profile(session_bias, entry_type)
        indicator_params = {}

    out = dict(signal)
    out["evaluation"] = {
        "action": action,
        "entry_mode": entry_type,
        "policy_score": round(score, 1),
        "mode": evaluation_mode(config),
        "reason": "; ".join(reason_parts),
        "recent_symbol_stats": recent,
        "scenario_key": mgmt.get("scenario_key"),
        "recipe_source": mgmt.get("source"),
        "regime": (regime_eff or {}).get("regime"),
        "regime_source": (regime_eff or {}).get("source"),
        "regime_min_confidence": (regime_eff or {}).get("min_confidence"),
        "regime_min_risk_reward": (regime_eff or {}).get("min_risk_reward"),
    }
    out["execution_policy"] = {
        "action": action,
        "entry_type": entry_type,
        "entry_offset_atr": float(mgmt.get("limit_offset_atr") or 0.0) if entry_type == "limit" else 0.0,
        "confidence_adjusted": int(min(99, max(0, (signal.get("confidence") or 50) + int((score - 50) / 5)))),
        "sl_atr_mult": mgmt.get("sl_atr_mult"),
        "tp1_r": mgmt.get("tp1_r"),
        "tp2_r": mgmt.get("tp2_r"),
    }
    out["management_profile"] = mgmt
    # Logged for trade_manager ghost experiments / later promotion.
    out["indicator_params"] = indicator_params
    if action == "skip":
        log.info(
            "Evaluation skip %s %s score=%.1f — %s",
            symbol,
            signal.get("side"),
            score,
            out["evaluation"]["reason"],
        )
    else:
        log.info(
            "Evaluation %s %s → %s score=%.1f BE=%.2fR trail=%.2fR scenario=%s src=%s",
            symbol,
            signal.get("side"),
            entry_type,
            score,
            float(mgmt.get("break_even_trigger_r") or 0),
            float(mgmt.get("trail_start_r") or 0),
            mgmt.get("scenario_key"),
            mgmt.get("source"),
        )
    return out


def evaluate_batch(
    candidates: list[dict[str, Any]],
    features: dict[str, Any],
    config: dict[str, Any],
    *,
    spread_data: dict[str, float] | None = None,
    recent_trades: list[dict[str, Any]] | None = None,
    logger: logging.Logger | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into evaluated (executable) and skipped."""
    evaluated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    feature_symbols = features.get("symbols") or {}
    for signal in candidates:
        sym = signal.get("symbol", "")
        feat = feature_symbols.get(sym) or {}
        spread = (spread_data or {}).get(sym)
        row = evaluate_candidate(
            signal,
            feat,
            config,
            spread_points=spread,
            recent_trades=recent_trades,
            logger=logger,
        )
        if row.get("evaluation", {}).get("action") == "skip":
            skipped.append(row)
        else:
            evaluated.append(row)
    return evaluated, skipped