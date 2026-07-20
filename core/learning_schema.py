"""Normalized event schemas for the learning loop (Phase 2.4).

Every bot action is reduced to one of a small set of categories so an LLM/ML
reviewer can reason about what happened. Schemas are deliberately flat dicts.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Canonical event categories written to logs/*.jsonl
EVENT_CATEGORIES = [
    "market_context",
    "signal_context",
    "order_decision",
    "execution_quality",
    "position_management",
    "outcome",
    "mistake_type",
    "learning_rating",
    "config_proposal",
    "config_patch_result",
]

# Decision-event fields (logs/decisions.jsonl)
DECISION_FIELDS = [
    "timestamp", "symbol", "timeframe", "mode", "price", "spread_points",
    "atr", "volatility_regime", "side", "confidence", "decision",
    "entry_type", "guards", "reason", "config_hash", "profile",
]

# Completed-trade review fields (logs/reviews.jsonl)
REVIEW_FIELDS = [
    "reviewed_at", "trade_id", "ticket", "symbol", "side", "setup",
    "entry_price", "exit_price", "sl", "tp1", "duration_seconds",
    "net_profit", "r_multiple", "mfe_R", "mae_R",
    "post_exit_max_favorable_atr", "post_exit_max_adverse_atr",
    "entry_timing_score", "exit_quality_score", "risk_score",
    "trend_alignment_score", "execution_score", "rating_total",
    "mistake_categories", "diagnosis", "suggested_actions",
    "suggested_config_changes", "mode",
]

# Mistake categories the reviewer can emit
MISTAKE_TYPES = [
    "tp_too_early", "tp_too_far", "sl_too_tight", "entry_late",
    "wrong_timeframe_alignment", "spread_spike_entry", "spread_spike_exit",
    "overtrading", "chop_zone_entry", "poor_limit_distance",
    "bad_session", "missed_trade_after_skip",
]

LEARNING_MODES = [
    "observe_only", "review_only", "propose_only",
    "shadow_apply", "live_apply_limited",
]


def config_snapshot_hash(config: dict[str, Any]) -> str:
    """Stable short hash of the config slices that affect trading decisions."""
    keys = (
        "evaluation", "signals", "trading", "filters", "fast_mode",
        "session_scoring", "quant", "risk", "execution",
    )
    snap = {k: config.get(k) for k in keys}
    payload = json.dumps(snap, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def build_decision_event(**fields: Any) -> dict[str, Any]:
    """Normalize a decision record, filling missing fields with None."""
    out: dict[str, Any] = {}
    for f in DECISION_FIELDS:
        out[f] = fields.get(f)
    out.setdefault("guards", {})
    return out
