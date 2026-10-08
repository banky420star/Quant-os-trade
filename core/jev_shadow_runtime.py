"""JEV decision instrumentation for Quant OS' existing evaluation stage.

Shadow: log JEV verdicts without filtering any candidate.
Paper gate: filter candidates ONLY with explicit paper-only configuration.
This module never sends orders and does not alter the MT5 execution path.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from core.jev_decision_bus import JevDecisionBus, normalize_candidate
from core.utils import (
    append_archive_record, read_json_state, utc_now_iso, write_json_state
)


def _candidate_side(candidate: dict[str, Any]) -> str:
    raw = str(candidate.get("side") or "").upper()
    return {"LONG": "BUY", "SHORT": "SELL"}.get(raw, raw)


def _fingerprint(state: dict[str, Any]) -> str:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _age_seconds(timestamp: Any) -> float:
    if not timestamp:
        return float("inf")
    try:
        dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def _effective_mode(config: dict[str, Any]) -> str:
    j = config.get("jev") or {}
    requested = str(j.get("mode", "shadow")).lower()
    ex = config.get("execution") or {}
    if (
        requested == "paper_gate"
        and ex.get("mode") == "paper"
        and ex.get("live_trading_enabled") is False
    ):
        return "paper_gate"
    return "shadow"


def evaluate_candidates(
    evaluated: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    features: dict[str, Any] | None = None,
    market_context: dict[str, Any] | None = None,
    positions: list[dict[str, Any]] | None = None,
    risk_state: dict[str, Any] | None = None,
    edge_scores: dict[str, Any] | None = None,
    recent_trades: list[dict[str, Any]] | None = None,
    bus: JevDecisionBus | None = None,
    persist: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Evaluate candidates and optionally veto in paper-only mode.

    Returns unchanged baseline candidates in shadow mode, filtered copies in
    paper_gate mode. An API failure never becomes a positive JEV endorsement.
    """
    j = config.get("jev") or {}
    requested_mode = str(j.get("mode") or "shadow")
    effective_mode = _effective_mode(config)
    if not bool(j.get("enabled", False)):
        return list(evaluated), {
            "timestamp": utc_now_iso(), "enabled": False,
            "mode": "off", "baseline_count": len(evaluated), "decisions": []
        }

    bus = bus or JevDecisionBus(config)
    max_calls = max(1, int(j.get("max_candidates_per_cycle", 5)))
    cache_ttl = max(0, int(j.get("cache_seconds", 15)))
    latest = read_json_state("jev_decisions.json", default={}) or {} if persist else {}
    latest_rows = {
        str(row.get("fingerprint")): row
        for row in latest.get("decisions", [])
        if isinstance(row, dict)
    }
    accepted: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    now = utc_now_iso()
    for i, candidate in enumerate(evaluated):
        state = normalize_candidate(
            candidate, features or {}, market_context or {}, positions,
            risk_state=risk_state, edge_scores=edge_scores,
            recent_trades=recent_trades,
        )
        fp = _fingerprint(state)
        side = _candidate_side(candidate)
        cached = latest_rows.get(fp)
        if i >= max_calls and (not cached or _age_seconds(cached.get("timestamp")) > cache_ttl):
            d = {
                "action": "WAIT", "probabilities": {"BUY": 0.0, "SELL": 0.0, "WAIT": 1.0},
                "confidence": 1.0, "provider": "budget_exceeded",
                "model": bus.model, "latency_ms": 0.0,
                "reason": "per-cycle inference budget",
            }
            reused = False
        elif cached and _age_seconds(cached.get("timestamp")) <= cache_ttl:
            d = dict(cached.get("decision") or {})
            reused = True
        else:
            try:
                d = bus.decide(state).as_dict()
            except Exception as exc:
                d = {
                    "action": "WAIT", "probabilities": {"BUY": 0.0, "SELL": 0.0, "WAIT": 1.0},
                    "confidence": 1.0, "provider": "error",
                    "model": bus.model, "latency_ms": 0.0,
                    "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                }
            reused = False

        # Trust only a real JEV response to authorize PAPER entries. A stub,
        # API failure or stale/over-budget verdict never approves trades.
        genuine = d.get("provider") == "openrouter"
        agrees = genuine and d.get("action") == side and side in {"BUY", "SELL"}
        paper_pass = agrees if effective_mode == "paper_gate" else True
        row = {
            "timestamp": now, "signal_id": candidate.get("signal_id"),
            "symbol": candidate.get("symbol"), "candidate_side": side,
            "candidate_setup": candidate.get("setup_type"), "fingerprint": fp,
            "state": state, "decision": d, "agreement": agrees,
            "mode": effective_mode,
            "paper_allowed": bool(paper_pass), "cached": reused,
        }
        audit.append(row)
        if paper_pass:
            accepted.append(dict(candidate))
        if persist and not reused:
            txid = hashlib.sha256((now + fp + str(i)).encode()).hexdigest()[:32]
            append_archive_record("jev_decision_archive.jsonl",
                {**row, "transaction_id": txid}, max_lines=20000)

    doc = {
        "timestamp": now, "enabled": True,
        "requested_mode": requested_mode, "mode": effective_mode,
        "baseline_count": len(evaluated), "count": len(audit),
        "accepted_count": len(accepted),
        "vetoed_count": len(evaluated) - len(accepted),
        "agreement_count": sum(bool(row["agreement"]) for row in audit),
        "real_provider_count": sum(
            row["decision"].get("provider") == "openrouter" for row in audit
        ), "decisions": audit,
    }
    if persist:
        write_json_state("jev_decisions.json", doc)
    return accepted, doc
