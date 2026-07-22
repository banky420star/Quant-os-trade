"""Learning review loop (Phase 2.4).

Reads recent closed trades, scores them, diagnoses mistakes, updates
state/learning_state.json, writes normalized reviews + bounded config proposals
to logs/*.jsonl. It NEVER places trades and (by default) NEVER modifies the
live config. Modes gate how far the pipeline goes:

  observe_only       -> log decisions only (no review, no proposal, no apply)
  review_only        -> score trades + update learning state
  propose_only       -> also write bounded config proposals
  shadow_apply       -> simulate proposals against a config copy (no live change)
  live_apply_limited -> apply tiny bounded patches to a runtime override file,
                        with rollback if performance gets worse

Default mode is observe_only. Live self-modification is blocked by default.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config_proposal import apply_patch_to_config, is_dangerous, propose_from_reviews
from core.learning_overrides import active_patches, read_overrides, record_patch_baseline, rollback_patch
from core.learning_logger import log_config_change, log_config_proposal, log_review
from core.learning_schema import LEARNING_MODES, config_snapshot_hash
from core.trade_reviewer import review_trade
from core.utils import (
    load_config,
    read_json_state,
    setup_logger,
    utc_now_iso,
    write_json_state,
)

STATE_FILE = "learning_state.json"
RUNTIME_OVERRIDES_FILE = "learning_config_overrides.json"

_LOGGER = None


def _logger():
    global _LOGGER
    if _LOGGER is None:
        _LOGGER = setup_logger("learning_review_loop", "learning_review_loop.log")
    return _LOGGER


def _mode(config: dict[str, Any]) -> str:
    m = str((config.get("learning") or {}).get("mode") or "observe_only")
    return m if m in LEARNING_MODES else "observe_only"


def _default_state() -> dict[str, Any]:
    return {
        "updated_at": utc_now_iso(),
        "mode": "observe_only",
        "last_reviewed_trade_id": None,
        "reviewed_count": 0,
        "rolling_win_rate_pct": 50.0,
        "rolling_expectancy_r": 0.0,
        "rolling_drawdown_pct": 0.0,
        "avg_rating_by_symbol": {},
        "avg_rating_by_setup": {},
        "mistake_counts": {},
        "recent_ratings": [],
        "active_proposals": [],
        "rejected_proposals": [],
        "applied_patches": [],
        "rollback_triggers": [],
    }


def read_state() -> dict[str, Any]:
    return read_json_state(STATE_FILE, default=_default_state()) or _default_state()


def _rolling_stats(reviews: list[dict[str, Any]]) -> tuple[float, float]:
    if not reviews:
        return 50.0, 0.0
    wins = sum(1 for r in reviews if (r.get("net_profit") or 0) > 0)
    wr = 100.0 * wins / len(reviews)
    rs = [r.get("r_multiple") for r in reviews if r.get("r_multiple") is not None]
    exp = sum(rs) / len(rs) if rs else 0.0
    return round(wr, 1), round(exp, 3)


def _update_state(state: dict[str, Any], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    all_ratings = list(state.get("recent_ratings") or []) + [r.get("rating_total") for r in reviews if r.get("rating_total") is not None]
    all_ratings = all_ratings[-200:]
    wr, exp = _rolling_stats(
        [{"net_profit": r.get("net_profit"), "r_multiple": r.get("r_multiple")} for r in reviews]
    )
    # blend with history for a rolling feel
    prev_wr = float(state.get("rolling_win_rate_pct") or 50.0)
    prev_exp = float(state.get("rolling_expectancy_r") or 0.0)
    state["rolling_win_rate_pct"] = round(0.7 * prev_wr + 0.3 * wr, 1) if reviews else prev_wr
    state["rolling_expectancy_r"] = round(0.7 * prev_exp + 0.3 * exp, 3) if reviews else prev_exp

    by_sym: dict[str, list[int]] = defaultdict(list)
    by_setup: dict[str, list[int]] = defaultdict(list)
    for r in reviews:
        if r.get("rating_total") is not None:
            if r.get("symbol"):
                by_sym[r["symbol"]].append(r["rating_total"])
            if r.get("setup"):
                by_setup[r["setup"]].append(r["rating_total"])
    sym_avg = state.get("avg_rating_by_symbol") or {}
    for s, vals in by_sym.items():
        sym_avg[s] = round(sum(vals) / len(vals), 1)
    setup_avg = state.get("avg_rating_by_setup") or {}
    for s, vals in by_setup.items():
        setup_avg[s] = round(sum(vals) / len(vals), 1)
    state["avg_rating_by_symbol"] = sym_avg
    state["avg_rating_by_setup"] = setup_avg

    counts = Counter(state.get("mistake_counts") or {})
    for r in reviews:
        for m in r.get("mistake_categories") or []:
            counts[m] += 1
    state["mistake_counts"] = dict(counts)
    state["recent_ratings"] = all_ratings
    state["reviewed_count"] = int(state.get("reviewed_count") or 0) + len(reviews)
    if reviews:
        state["last_reviewed_trade_id"] = reviews[-1].get("trade_id")
    state["updated_at"] = utc_now_iso()
    return state



def _parse_iso(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _post_exit_prices(trade: dict[str, Any], candles_doc: dict[str, Any]) -> list[float]:
    """Favourable post-exit M5 prices for a closed trade (highs for BUY, lows for SELL).

    Reads state/latest_candles.json (written by data_loop) and returns the
    sequence of favourable-extreme prices for candles strictly after the exit
    time, so the reviewer can detect tp_too_early from real post-exit movement.
    """
    symbol = trade.get("symbol")
    side = str(trade.get("side") or "").upper()
    closed = _parse_iso(trade.get("closed_at"))
    if not symbol or not closed or side not in ("BUY", "SELL"):
        return []
    sym_rows = (candles_doc.get("symbols") or {}).get(symbol) or {}
    candles = sym_rows.get("M5") or sym_rows.get("M15") or []
    key = "high" if side == "BUY" else "low"
    out: list[float] = []
    for c in candles:
        ts = _parse_iso(c.get("time"))
        if ts and ts > closed:
            try:
                out.append(float(c.get(key)))
            except (TypeError, ValueError):
                continue
    return out[:24]  # cap to ~2h of M5 candles


def _check_rollbacks(state: dict[str, Any]) -> dict[str, Any]:
    """Roll back applied patches whose post-apply rolling expectancy degraded."""
    overrides = read_overrides()
    cur_exp = float(state.get("rolling_expectancy_r") or 0.0)
    reviewed = int(state.get("reviewed_count") or 0)
    for p in list(overrides.get("patches") or []):
        baseline = p.get("baseline_expectancy_r")
        if baseline is None:
            continue
        # need enough new reviews since apply before judging a patch
        if reviewed < int(p.get("applied_at_reviewed") or 0) + 5:
            continue
        if cur_exp < float(baseline) - 0.1:
            rollback_patch(p.get("proposal_id"), f"expectancy_degraded {cur_exp:.3f}<{baseline:.3f}")
            state.setdefault("rollback_triggers", []).append({
                "proposal_id": p.get("proposal_id"),
                "reason": f"expectancy {cur_exp:.3f} < baseline {baseline:.3f} - 0.1",
                "at": __import__("core.utils", fromlist=["utc_now_iso"]).utc_now_iso(),
            })
            log_config_change({
                "action": "rollback",
                "proposal_id": p.get("proposal_id"),
                "reason": "expectancy_degraded",
            })
    state["rollback_triggers"] = (state.get("rollback_triggers") or [])[-50:]
    return state

def _new_closes(state: dict[str, Any], trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    last = state.get("last_reviewed_trade_id")
    if not last:
        return trades[-25:]
    # trades are sorted newest-first (descending trade_id/closed_at).
    # New closes appear at the front of the list. Collect every trade
    # from index 0 until (not including) the one matching `last`.
    out: list[dict[str, Any]] = []
    for t in trades:
        if str(t.get("trade_id")) == str(last):
            break
        out.append(t)
    # Fallback for log rotation: when `last` genuinely vanished from the
    # list, ghost-scroll the 25 most recent trades to avoid re-reviewing
    # the entire trade log every cycle.
    if trades:
        last_exists = any(str(t.get("trade_id")) == str(last) for t in trades[:50])
        if not last_exists:
            out = trades[-25:]  # log rotation: cap batch size
    return out


def _shadow_simulate(proposals: list[dict[str, Any]], config: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    """Mark proposals as shadow-tested (no live change). Records simulated config hash."""
    for p in proposals:
        if p.get("rejected"):
            continue
        simulated = apply_patch_to_config(config, p.get("patch") or {})
        p["shadow_config_hash"] = config_snapshot_hash(simulated)
        p["shadow_tested"] = True
    return proposals


def _apply_limited(proposals: list[dict[str, Any]], config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Apply only low-risk, auto_apply_allowed proposals to a runtime override file.

    Never edits config.yaml. Never applies dangerous/rejected proposals. Records
    a rollback marker so the loop can revert if performance later degrades.
    """
    overrides = read_json_state(RUNTIME_OVERRIDES_FILE, default={"patches": []}) or {"patches": []}
    applied = list(overrides.get("patches") or [])
    for p in proposals:
        if p.get("rejected") or not p.get("auto_apply_allowed"):
            continue
        dangerous, why = is_dangerous(p.get("patch") or {}, reviewed_trades=int((p.get("evidence") or {}).get("reviewed_trades", 0)))
        if dangerous:
            p["rejected"] = True
            p["rejection_reason"] = why
            continue
        applied.append({
            "proposal_id": p.get("proposal_id"),
            "applied_at": utc_now_iso(),
            "applied_at_reviewed": int(state.get("reviewed_count") or 0),
            "patch": p.get("patch"),
            "evidence": p.get("evidence"),
            "config_hash_before": config_snapshot_hash(config),
        })
        log_config_change({
            "action": "apply_limited",
            "proposal_id": p.get("proposal_id"),
            "patch": p.get("patch"),
            "config_hash_before": config_snapshot_hash(config),
            "auto_apply": True,
        })
        state.setdefault("applied_patches", []).append(p.get("proposal_id"))
    overrides["patches"] = applied[-50:]
    overrides["updated_at"] = utc_now_iso()
    write_json_state(RUNTIME_OVERRIDES_FILE, overrides)
    return state


def run() -> dict | None:
    config = load_config()
    logger = _logger()
    mode = _mode(config)
    state = read_state()
    state["mode"] = mode

    # observe_only: only the decision logger (called elsewhere) runs; no review.
    if mode == "observe_only":
        state["updated_at"] = utc_now_iso()
        write_json_state(STATE_FILE, state)
        logger.info("Learning loop observe_only — no review/proposal/apply.")
        return state

    tl = read_json_state("trade_log.json", default=[]) or []
    trades = list(tl if isinstance(tl, list) else (tl or {}).get("trades") or [])
    new_trades = _new_closes(state, trades)
    if not new_trades:
        write_json_state(STATE_FILE, state)
        logger.info("Learning loop: no new closes to review (mode=%s).", mode)
        return state

    candles_doc = read_json_state("latest_candles.json", default={}) or {}
    reviews: list[dict[str, Any]] = []
    for t in new_trades:
        rev = review_trade(t, post_exit_prices=_post_exit_prices(t, candles_doc), config=config)
        rev["mode"] = mode
        reviews.append(rev)
        log_review(rev)

    state = _update_state(state, reviews)

    if mode in ("propose_only", "shadow_apply", "live_apply_limited"):
        proposals = propose_from_reviews(reviews, config)
        for p in proposals:
            log_config_proposal(p)
            if p.get("rejected"):
                state.setdefault("rejected_proposals", []).append({
                    "proposal_id": p.get("proposal_id"), "reason": p.get("rejection_reason"),
                })
            else:
                state.setdefault("active_proposals", []).append(p.get("proposal_id"))
        state["active_proposals"] = list(dict.fromkeys(state.get("active_proposals") or []))[-50:]
        state["rejected_proposals"] = (state.get("rejected_proposals") or [])[-50:]

        if mode in ("shadow_apply", "live_apply_limited"):
            proposals = _shadow_simulate(proposals, config, state)
        if mode == "live_apply_limited":
            state = _apply_limited(proposals, config, state)
            # stamp baselines for newly applied patches + roll back degraded ones
            for pid in state.get("applied_patches", [])[-3:]:
                record_patch_baseline(pid, float(state.get("rolling_expectancy_r") or 0.0))
            state = _check_rollbacks(state)

    write_json_state(STATE_FILE, state)
    logger.info(
        "Learning loop: reviewed %d trades (mode=%s) -> win=%.1f%% exp=%.3fR mistakes=%s",
        len(reviews), mode, state.get("rolling_win_rate_pct"), state.get("rolling_expectancy_r"),
        state.get("mistake_counts"),
    )
    return state


if __name__ == "__main__":
    run()
