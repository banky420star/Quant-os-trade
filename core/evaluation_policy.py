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
    return {
        "entry_type": entry_type,
        "limit_offset_atr": float(session_bias.get("limit_offset_atr") or 0.1),
        "sl_model": "structure_atr",
        "sl_atr_mult": float(session_bias.get("sl_atr_mult") or 1.15),
        "tp_model": "rr",
        "tp1_r": float(session_bias.get("tp1_r") or 0.95),
        "tp2_r": float(session_bias.get("tp2_r") or 1.25),
        "break_even_enabled": True,
        "break_even_trigger_r": float(session_bias.get("break_even_trigger_r") or 0.4),
        "break_even_lock_r": 0.05,
        "trailing_enabled": True,
        "trail_start_r": float(session_bias.get("trail_start_r") or 0.65),
        "trail_atr_mult": float(session_bias.get("trail_atr_mult") or 0.43),
        "max_hold_minutes": int(session_bias.get("max_hold_minutes") or 20),
        "cancel_if_not_filled_seconds": int(session_bias.get("cancel_if_not_filled_seconds") or 110),
    }


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
    session_bias = session_entry_bias(str(session or "unknown"))
    recent = recent_symbol_stats(list(recent_trades or []), symbol)

    score, reason_parts = compute_policy_score(
        signal,
        feat,
        spread_points=spread_points,
        recent=recent,
        session_bias=session_bias,
    )

    skip_below = float(cfg.get("skip_below_score") or 25)
    min_score = float(cfg.get("min_policy_score") or 35)
    action = "execute"
    if score < skip_below:
        action = "skip"
    elif score < min_score:
        action = "skip"

    entry_type = _pick_entry_type(signal, feat, session_bias, score)
    if action != "skip" and entry_type == "limit" and signal.get("within_reach") is False:
        action = "skip"
        reason_parts.append("limit_unreachable")

    mgmt = _build_management_profile(session_bias, entry_type)
    out = dict(signal)
    out["evaluation"] = {
        "action": action,
        "entry_mode": entry_type,
        "policy_score": round(score, 1),
        "mode": evaluation_mode(config),
        "reason": "; ".join(reason_parts),
        "recent_symbol_stats": recent,
    }
    out["execution_policy"] = {
        "action": action,
        "entry_type": entry_type,
        "entry_offset_atr": mgmt["limit_offset_atr"] if entry_type == "limit" else 0.0,
        "confidence_adjusted": int(min(99, max(0, (signal.get("confidence") or 50) + int((score - 50) / 5)))),
    }
    out["management_profile"] = mgmt
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
            "Evaluation %s %s → %s score=%.1f BE=%.2fR trail=%.2fR",
            symbol,
            signal.get("side"),
            entry_type,
            score,
            mgmt["break_even_trigger_r"],
            mgmt["trail_start_r"],
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