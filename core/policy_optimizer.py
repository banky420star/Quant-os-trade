"""Policy optimizer — pick best execution policy per symbol/setup/session with sample gates."""

from __future__ import annotations

import logging
from typing import Any

from core.strategy_policy import KNOWN_SETUPS, normalize_setup_type
from core.trade_history import trade_history_filename
from core.utils import read_json_state, utc_now_iso, write_json_state

BEST_POLICIES_FILE = "best_policies.json"
POLICY_SCORES_FILE = "policy_scores.json"


def policy_optimizer_enabled(config: dict[str, Any]) -> bool:
    return bool((config.get("policy_optimizer") or {}).get("enabled", False))


def policy_optimizer_mode(config: dict[str, Any]) -> str:
    return str((config.get("policy_optimizer") or {}).get("mode") or "shadow")


def _optimizer_cfg(config: dict[str, Any]) -> dict[str, Any]:
    cfg = config.get("policy_optimizer") or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "mode": str(cfg.get("mode") or "shadow"),
        "min_trades_suggest": int(cfg.get("min_trades_suggest", 10)),
        "min_trades_soft": int(cfg.get("min_trades_soft", 30)),
        "min_trades_auto": int(cfg.get("min_trades_auto", 50)),
        "suspect_exit_lookback": int(cfg.get("suspect_exit_lookback", 50)),
    }


def policy_cell_key(symbol: str, setup: str, session: str) -> str:
    """Canonical optimizer cell: symbol|setup|session."""
    return f"{symbol}|{setup or 'unknown'}|{session or 'unknown'}"


def _session_from_trade(trade: dict[str, Any]) -> str:
    meta = trade.get("signal_meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    mc = trade.get("market_context") or meta.get("market_context") or {}
    if not isinstance(mc, dict):
        mc = {}
    return str(mc.get("session") or meta.get("session") or "unknown")


def _setup_from_trade(trade: dict[str, Any]) -> str:
    meta = trade.get("signal_meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    mc = trade.get("market_context") or meta.get("market_context") or {}
    if not isinstance(mc, dict):
        mc = {}
    return normalize_setup_type(
        trade.get("setup_type") or meta.get("setup_type"),
        meta=meta,
        market_context=mc,
    )


def trade_policy_cell(trade: dict[str, Any]) -> str | None:
    """Map a closed trade to optimizer cell key, or None when incomplete."""
    symbol = trade.get("symbol")
    if not symbol:
        return None
    if trade.get("archive_polluted"):
        return None
    setup = _setup_from_trade(trade)
    if not setup or setup not in KNOWN_SETUPS:
        return None
    session = _session_from_trade(trade)
    return policy_cell_key(str(symbol), setup, session)


def count_samples_by_cell(trades: list[dict[str, Any]]) -> dict[str, int]:
    """Count paper trades per symbol|setup|session cell."""
    counts: dict[str, int] = {}
    for trade in trades:
        cell = trade_policy_cell(trade)
        if not cell:
            continue
        counts[cell] = counts.get(cell, 0) + 1
    return counts


def exit_labels_suspect(
    trades: list[dict[str, Any]],
    *,
    lookback: int = 50,
) -> bool:
    """True when recent closes show loss + take_profit mis-labelling."""
    recent = list(trades or [])[-max(1, lookback):]
    for trade in recent:
        if trade.get("archive_polluted"):
            continue
        reason = str(trade.get("exit_reason") or "").lower()
        if "take_profit" not in reason:
            continue
        pnl = float(trade.get("pnl") or 0)
        result = str(trade.get("result") or "").lower()
        if pnl < 0 or result == "loss":
            return True
    return False


def resolve_gate(
    sample_n: int,
    mode: str,
    *,
    min_suggest: int,
    min_soft: int,
    min_auto: int,
    auto_blocked: bool = False,
) -> str:
    """Map sample count + optimizer mode to observe|suggest|soft|auto."""
    if sample_n < min_suggest:
        return "observe"
    if auto_blocked or mode == "shadow":
        if sample_n >= min_soft:
            return "soft"
        return "suggest"
    if mode == "suggest":
        if sample_n >= min_soft:
            return "soft"
        return "suggest"
    if sample_n >= min_auto:
        return "auto"
    if sample_n >= min_soft:
        return "soft"
    return "suggest"


def _gate_reason(
    gate: str,
    sample_n: int,
    *,
    mode: str,
    auto_blocked: bool,
    best_score: float,
) -> str:
    parts = [f"sample_n={sample_n}", f"mode={mode}", f"best_score={best_score:.1f}"]
    if auto_blocked:
        parts.append("auto_blocked_exit_labels_suspect")
    if gate == "observe":
        parts.append("insufficient_samples")
    return "; ".join(parts)


def _extract_policy_candidates(scores_doc: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Normalize policy_scores.json into cell -> [candidate policies].

    Handles three documented shapes:
      1. ``policies``/``cells``: dict keyed by cell -> variants list/single.
      2. ``variants``: a FLAT top-level list of variant dicts each carrying
         ``symbol``/``setup_type``/``session`` — grouped into cells here.
         (This is the shape the live scorer writes: 576 variants, 48 cells.)
      3. ``best_by_key``: dict keyed by cell -> single best variant dict —
         used as a last-resort fallback if ``variants`` is absent.
    """
    out: dict[str, list[dict[str, Any]]] = {}

    # Shape 1 — explicit cell-keyed dict.
    raw = scores_doc.get("policies") or scores_doc.get("cells") or {}
    if isinstance(raw, dict):
        for cell, payload in raw.items():
            if not isinstance(payload, (list, dict)):
                continue
            if isinstance(payload, list):
                variants = [v for v in payload if isinstance(v, dict)]
            else:
                nested = payload.get("variants") or payload.get("candidates") or payload.get("policies")
                if isinstance(nested, list):
                    variants = [v for v in nested if isinstance(v, dict)]
                elif payload.get("entry_type") or payload.get("management_profile"):
                    variants = [payload]
                else:
                    variants = []
            if variants:
                out[str(cell)] = variants

    # Shape 2 — flat variants list, group by symbol|setup|session.
    if not out:
        flat = scores_doc.get("variants")
        if isinstance(flat, list):
            grouped: dict[str, list[dict[str, Any]]] = {}
            for v in flat:
                if not isinstance(v, dict):
                    continue
                symbol = v.get("symbol")
                if not symbol:
                    continue
                setup = normalize_setup_type(v.get("setup_type"))
                session = str(v.get("session") or "unknown")
                cell = policy_cell_key(str(symbol), setup, session)
                grouped.setdefault(cell, []).append(v)
            out = grouped

    # Shape 3 — best_by_key fallback (cell -> single best variant).
    if not out:
        bbk = scores_doc.get("best_by_key")
        if isinstance(bbk, dict):
            for cell, payload in bbk.items():
                if isinstance(payload, dict):
                    out[str(cell)] = [payload]

    return out


def pick_best_policy(
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the highest policy_score variant from one cell's candidates."""
    if not candidate:
        return None
    if candidate.get("entry_type") or candidate.get("management_profile"):
        return candidate
    variants = candidate.get("variants") or candidate.get("candidates") or candidate.get("policies")
    if not isinstance(variants, list) or not variants:
        return None
    best: dict[str, Any] | None = None
    best_score = float("-inf")
    for row in variants:
        if not isinstance(row, dict):
            continue
        score = float(row.get("policy_score") or row.get("score") or 0)
        if score > best_score:
            best_score = score
            best = row
    return best


def build_best_policies(
    policy_scores: dict[str, Any],
    trades: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Compute best_policies.json payload from scores + paper trade samples."""
    log = logger or logging.getLogger("policy_optimizer")
    cfg = _optimizer_cfg(config)
    mode = cfg["mode"]
    sample_counts = count_samples_by_cell(trades)
    auto_blocked = exit_labels_suspect(trades, lookback=cfg["suspect_exit_lookback"])
    if auto_blocked:
        log.warning("Exit labels suspect (loss+take_profit) — auto gate disabled")

    candidates_by_cell = _extract_policy_candidates(policy_scores)
    policies: dict[str, dict[str, Any]] = {}

    for cell, variants in candidates_by_cell.items():
        best = pick_best_policy({"variants": variants})
        if not best:
            continue
        sample_n = int(sample_counts.get(cell, 0))
        score = float(best.get("policy_score") or best.get("score") or 0)
        gate = resolve_gate(
            sample_n,
            mode,
            min_suggest=cfg["min_trades_suggest"],
            min_soft=cfg["min_trades_soft"],
            min_auto=cfg["min_trades_auto"],
            auto_blocked=auto_blocked,
        )
        entry_type = str(
            best.get("entry_type")
            or (best.get("management_profile") or {}).get("entry_type")
            or "limit"
        )
        mgmt = dict(best.get("management_profile") or {})
        if entry_type and "entry_type" not in mgmt:
            mgmt["entry_type"] = entry_type

        policies[cell] = {
            "entry_type": entry_type,
            "management_profile": mgmt,
            "policy_score": round(score, 1),
            "sample_n": sample_n,
            "gate": gate,
            "reason": _gate_reason(gate, sample_n, mode=mode, auto_blocked=auto_blocked, best_score=score),
        }

    return {
        "timestamp": utc_now_iso(),
        "mode": mode,
        "auto_blocked": auto_blocked,
        "policies": policies,
        "source": "policy_optimizer",
        "cells_scored": len(candidates_by_cell),
        "cells_selected": len(policies),
    }


def run(
    config: dict[str, Any] | None = None,
    *,
    logger: logging.Logger | None = None,
) -> dict[str, Any] | None:
    """Read policy_scores + paper_trades, write best_policies.json (+ SQLite kv)."""
    from core.utils import load_config

    cfg_root = config or load_config()
    log = logger or logging.getLogger("policy_optimizer")
    opt_cfg = _optimizer_cfg(cfg_root)

    if not opt_cfg["enabled"]:
        log.debug("Policy optimizer disabled")
        return None

    policy_scores = read_json_state(POLICY_SCORES_FILE, default={}) or {}
    trades_doc = read_json_state(trade_history_filename(cfg_root), default={"trades": []}) or {}
    trades = list(trades_doc.get("trades") or [])

    doc = build_best_policies(policy_scores, trades, cfg_root, logger=log)
    write_json_state(BEST_POLICIES_FILE, doc)

    try:
        from core.state_store import sync_store_from_doc

        sync_store_from_doc(cfg_root, "best_policies", doc)
    except Exception as exc:  # noqa: BLE001
        log.warning("best_policies SQLite sync skipped: %s", exc)

    log.info(
        "Policy optimizer: %d cells scored → %d policies (mode=%s auto_blocked=%s)",
        doc.get("cells_scored", 0),
        doc.get("cells_selected", 0),
        doc.get("mode"),
        doc.get("auto_blocked"),
    )
    return doc