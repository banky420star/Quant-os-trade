"""Explainable AI — human-readable decision breakdown."""

from __future__ import annotations

from typing import Any

from core.setup_library import enrich_setup_stats


def build_explain_report(
    signal: dict[str, Any],
    edge_scores: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured 'why' report for a candidate signal."""
    votes = signal.get("confidence_tree", {})
    evidence = signal.get("evidence", {})
    ctx = signal.get("market_context", {})
    regime = ctx.get("market_regime", {})
    setup_stats = enrich_setup_stats(
        signal.get("setup_type", "unknown"),
        edge_scores or {},
        signal.get("symbol"),
    )

    evidence_rows = []
    for key, val in evidence.items():
        pct = int(float(val) * 100) if isinstance(val, (int, float)) else 0
        evidence_rows.append({"name": key.replace("_", " ").title(), "score": pct})

    engine_rows = []
    for key, val in votes.items():
        engine_rows.append({
            "name": key.replace("_engine", "").replace("_", " ").title(),
            "score": int(val),
        })

    return {
        "signal_id": signal.get("signal_id"),
        "symbol": signal.get("symbol"),
        "side": signal.get("side"),
        "setup_type": signal.get("setup_type"),
        "confidence": signal.get("confidence"),
        "summary": f"{signal.get('side')} {signal.get('symbol')} — {signal.get('setup_type', '').replace('_', ' ')} ({signal.get('confidence')}%)",
        "evidence": evidence_rows,
        "engines": engine_rows,
        "reasons": signal.get("reasons", [signal.get("reason", "")]),
        "market_regime": {
            "primary": regime.get("primary") or ctx.get("regime"),
            "bias": regime.get("bias"),
            "description": regime.get("description"),
            "session": ctx.get("session"),
        },
        "setup_stats": setup_stats,
        "levels": {
            "entry": signal.get("entry"),
            "sl": signal.get("sl"),
            "tp1": signal.get("tp1"),
            "tp2": signal.get("tp2"),
            "market_price": signal.get("market_price"),
            "entry_mode": signal.get("entry_mode"),
            "entry_anchor": signal.get("entry_anchor"),
            "entry_anchor_price": signal.get("entry_anchor_price"),
            "entry_reason": signal.get("entry_reason"),
            "distance_atr": signal.get("distance_atr"),
        },
        "vetoes": signal.get("consensus_vetoes", []),
        "trade_score": signal.get("trade_score"),
    }