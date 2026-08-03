"""Paper-only validation for per-symbol adaptive proposals.

The symbol learner emits bounded proposals, but a proposal must earn forward
paper evidence before it can be considered for promotion.  This module scores
only explicitly tagged paper trades and never changes live configuration,
position sizing, or orders.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.reward_engine import reward_score
from core.utils import read_json_state, utc_now_iso, write_json_state

STATE_FILE = "adaptive_symbol_validation.json"
ACTIVE_OVERRIDES_FILE = "adaptive_symbol_overrides.json"


def proposal_id(symbol: str, proposed: dict[str, Any]) -> str:
    """Return a stable ID so future paper routing can label outcomes."""
    payload = json.dumps(proposed or {}, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha1(f"{symbol}:{payload}".encode("utf-8")).hexdigest()[:12]
    return f"{symbol}_{digest}"


def _trade_proposal_id(trade: dict[str, Any]) -> str | None:
    """Read only the explicit adaptive-symbol tag.

    Generic proposal IDs belong to other learning experiments and must not be
    mistaken for forward evidence for a symbol proposal.
    """
    value = trade.get("adaptive_symbol_proposal_id")
    if value:
        return str(value)
    meta = trade.get("signal_meta")
    if isinstance(meta, dict) and meta.get("adaptive_symbol_proposal_id"):
        return str(meta["adaptive_symbol_proposal_id"])
    return None


def _cfg(config: dict[str, Any]) -> dict[str, Any]:
    learning = config.get("self_learning") or {}
    adaptive = learning.get("adaptive_symbol_learning") or {}
    return adaptive.get("validation") or {}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def annotate_paper_validation_signals(
    approved: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Deterministically split paper signals into control and candidate arms.

    Only paper-mode signals are touched. Candidate routing applies the bounded
    proposed risk multiplier through the existing conviction sizing hook and
    stamps an explicit proposal ID that survives into the closed trade. MT5
    signals are returned unchanged, so this cannot alter live execution.
    """
    if str((config.get("execution") or {}).get("mode") or "paper").lower() != "paper":
        return approved
    proposals = read_json_state("adaptive_symbol_learning.json", default={}) or {}
    symbols = proposals.get("symbols") or {}
    if not isinstance(symbols, dict):
        return approved

    routed: list[dict[str, Any]] = []
    for record in approved:
        signal = record.get("signal", record) if isinstance(record, dict) else {}
        if not isinstance(signal, dict):
            routed.append(record)
            continue
        symbol = str(signal.get("symbol") or "")
        proposal = symbols.get(symbol) or {}
        if proposal.get("status") != "shadow_candidate":
            routed.append(record)
            continue
        proposed = proposal.get("proposed") or {}
        pid = proposal_id(symbol, proposed)
        signal_id = str(signal.get("signal_id") or "")
        # Stable 50/50 assignment: repeated cycles keep a signal in one arm,
        # while the ordinary untagged half remains the same-symbol control.
        bucket = int(hashlib.sha1(signal_id.encode("utf-8")).hexdigest()[:8], 16) % 2
        if bucket:
            routed.append(record)
            continue
        candidate = dict(signal)
        candidate["adaptive_symbol_proposal_id"] = pid
        candidate["adaptive_validation_arm"] = "candidate"
        base_multiplier = _float(candidate.get("conviction_size_mult"), 1.0)
        proposed_multiplier = _float(proposed.get("risk_multiplier"), 1.0)
        candidate["conviction_size_mult"] = round(
            max(0.5, min(1.10, base_multiplier * proposed_multiplier)),
            4,
        )
        if "signal" in record:
            routed.append({**record, "signal": candidate})
        else:
            routed.append(candidate)
    return routed


def _score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Score only rows with an explicit, finite realized R-multiple."""
    valid: list[dict[str, Any]] = []
    for row in rows:
        try:
            value = float(row.get("r_multiple"))
        except (TypeError, ValueError):
            continue
        if value == value and abs(value) != float("inf"):
            valid.append(row)
    return reward_score(valid)


def _validate_one(
    symbol: str,
    proposal: dict[str, Any],
    trades: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    proposed = proposal.get("proposed") or {}
    pid = proposal_id(symbol, proposed)
    symbol_trades = [t for t in trades if str(t.get("symbol") or "") == symbol]
    candidate = [t for t in symbol_trades if _trade_proposal_id(t) == pid]
    control = [t for t in symbol_trades if not _trade_proposal_id(t)]
    candidate_score = _score(candidate)
    control_score = _score(control)
    margin = round(candidate_score["score"] - control_score["score"], 4)
    min_trades = max(1, int(cfg.get("min_forward_trades", 25)))
    min_control = max(1, int(cfg.get("min_control_trades", 5)))
    min_margin = _float(cfg.get("min_reward_margin", 0.30), 0.30)
    min_exp = _float(cfg.get("min_expectancy_r", 0.0), 0.0)

    if candidate_score["n"] < min_trades:
        status = "awaiting_forward_data"
        reason = f"candidate has {candidate_score['n']}/{min_trades} tagged paper trades"
    elif control_score["n"] < min_control:
        status = "awaiting_control_data"
        reason = f"control has {control_score['n']}/{min_control} explicit-R paper trades"
    elif candidate_score["expectancy_r"] <= min_exp:
        status = "rejected"
        reason = f"candidate expectancy {candidate_score['expectancy_r']:+.3f}R is not above {min_exp:+.3f}R"
    elif margin < min_margin:
        status = "rejected"
        reason = f"candidate reward margin {margin:+.3f} is below {min_margin:+.3f}"
    else:
        status = "promotion_candidate"
        reason = f"candidate beats control by {margin:+.3f} reward points"

    return {
        "symbol": symbol,
        "proposal_id": pid,
        "status": status,
        "reason": reason,
        "candidate": candidate_score,
        "control": control_score,
        "reward_margin": margin,
        "gates": {
            "min_forward_trades": min_trades,
            "min_control_trades": min_control,
            "min_reward_margin": min_margin,
            "min_expectancy_r": min_exp,
        },
        "safety": {
            "paper_only": True,
            "live_config_changed": False,
            "orders_placed": 0,
            "operator_approval_required": True,
        },
    }


def validate_symbol_proposals(
    config: dict[str, Any],
    proposals_doc: dict[str, Any] | None = None,
    paper_trades: list[dict[str, Any]] | None = None,
    *,
    rollback_recommended: bool = False,
) -> dict[str, Any]:
    """Score adaptive proposals against tagged forward-paper outcomes.

    Untagged paper trades are used only as the same-symbol control. No live/MT5
    ledger is read, so a live account can never be contaminated by paper data.
    """
    cfg = _cfg(config)
    execution = config.get("execution") or {}
    mode = str(execution.get("mode") or "paper").lower()

    # Rollback is safety-critical and must run even when the active execution
    # mode is MT5. It only clears the separate, operator-approved override
    # artifact; it never touches config.yaml or places an order.
    rolled_back = False
    if rollback_recommended:
        active = read_json_state(ACTIVE_OVERRIDES_FILE, default={}) or {}
        if active.get("symbols"):
            rolled_back = True
            write_json_state(ACTIVE_OVERRIDES_FILE, {
                "timestamp": utc_now_iso(),
                "symbols": {},
                "status": "rolled_back",
                "reason": "learning_monitor_degrading",
            })

    if mode != "paper":
        return {
            "timestamp": utc_now_iso(),
            "mode": "paper_validation",
            "status": "skipped_live_mode",
            "symbols": {},
            "promotion_candidates": [],
            "rolled_back": rolled_back,
            "safety": {
                "paper_only": True,
                "live_config_changed": False,
                "orders_placed": 0,
                "operator_approval_required": True,
            },
            "note": "Forward validation is disabled outside paper mode; MT5 outcomes never consume paper evidence.",
        }
    if proposals_doc is None:
        proposals_doc = read_json_state("adaptive_symbol_learning.json", default={}) or {}
    if paper_trades is None:
        paper_doc = read_json_state("paper_trades.json", default={"trades": []}) or {}
        paper_trades = list(paper_doc.get("trades") or []) if isinstance(paper_doc, dict) else []
    symbols = proposals_doc.get("symbols") or {}
    results: dict[str, dict[str, Any]] = {}
    for symbol, proposal in symbols.items():
        if not isinstance(proposal, dict):
            continue
        results[str(symbol)] = _validate_one(str(symbol), proposal, paper_trades, cfg)

    if rollback_recommended:
        for row in results.values():
            if row["status"] not in {"awaiting_forward_data"}:
                row["status"] = "rollback_hold"
                row["reason"] = "learning monitor recommends rollback; promotion held"

    return {
        "timestamp": utc_now_iso(),
        "mode": "paper_validation",
        "status": "rollback_hold" if rollback_recommended else "active",
        "symbols": results,
        "promotion_candidates": [
            row["symbol"] for row in results.values() if row["status"] == "promotion_candidate"
        ],
        "rolled_back": rolled_back,
        "safety": {
            "paper_only": True,
            "live_config_changed": False,
            "orders_placed": 0,
            "operator_approval_required": True,
        },
        "note": "Promotion candidates require operator approval and a separate gated deployment path.",
    }


def run_adaptive_symbol_validation(
    config: dict[str, Any],
    *,
    proposals_doc: dict[str, Any] | None = None,
    rollback_recommended: bool = False,
    persist: bool = True,
) -> dict[str, Any]:
    """Run the paper validator and persist its audit state."""
    cfg = _cfg(config)
    if not cfg.get("enabled", True):
        output = {"timestamp": utc_now_iso(), "mode": "disabled", "symbols": {}}
    else:
        output = validate_symbol_proposals(
            config,
            proposals_doc=proposals_doc,
            rollback_recommended=rollback_recommended,
        )
    if persist:
        write_json_state(STATE_FILE, output)
    return output
