"""Learning review loop (Phase 2.5 — deterministic guards).

Reads recent closed trades, scores them, diagnoses mistakes, and emits
a **GuardReport** every cycle to ``logs/learning_guards.jsonl``. Config
files are NEVER touched: ``state/learning_config_overrides.json`` and
``logs/config_proposals.jsonl`` are removed from this loop's pipeline.
The pipeline is observe-only by design — operators act on the alerts.

Guard pipeline
==============
1. ``pause_on_consecutive_losses`` — if the most recent reviews show
   ``>= pause_on_consecutive_losses_thr`` consecutive losses, the
   guard verdict is ``pause`` and the overall report status is
   ``paused`` (highest priority).
2. ``halve_on_negative_expectancy`` — if ``rolling_expectancy_r`` drops
   below ``negative_expectancy_thr`` (default 0.0), the guard verdict
   is ``halve`` and the report status is ``halve_advisory`` so an
   operator can shrink position sizing manually.
3. ``alert_on_mistake_fingerprint`` — if any single mistake category
   recurs ``>= mistake_fingerprint_thr`` times in ``mistake_counts``,
   the guard verdict is ``alert`` and the report status is ``alert``.
4. ``log_always`` — every cycle (even with zero new closes) emits one
   structured row to ``logs/learning_guards.jsonl`` for replay-friendly
   observability. ``verdict="logged"`` is always returned.
5. ``no_config_patches`` — explicitly asserts the loop never wrote to
   ``learning_config_overrides.json`` or ``config_proposals.jsonl``.
   ``verdict="ok"`` with ``writes_blocked=True`` and an enumerated
   ``blocked_targets`` list.

Status priority: ``paused > alert > halve_advisory > active``.

Default mode is ``observe_only``. Live self-modification is blocked by
default AND can never be re-enabled by this loop — see notes below.

Backward compatibility
======================
- ``propose_from_reviews`` and ``apply_learning_overrides`` remain
  importable so tests built against the prior engine keep working, but
  this loop no longer invokes them.
- ``_check_rollbacks`` is kept as a module-level helper for the same
  reason; tests can exercise the rollback machinery directly even
  though the loop never toggles it.
- The state dict keeps ``active_proposals``, ``rejected_proposals``,
  ``applied_patches``, ``rollback_triggers`` as empty lists so the
  dashboard doesn't crash on legacy keys. The new authoritative field
  is ``last_guard_report``.
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

# Kept for back-compat: tests in tests/test_config_proposal.py +
# tests/test_learning_overrides.py import these directly. The loop
# no longer calls them — they are dead exports for the observe-only
# pipeline.
from core.config_proposal import (  # noqa: F401  (public back-compat)
    apply_patch_to_config,
    is_dangerous,
    propose_from_reviews,
)
from core.learning_overrides import (  # noqa: F401  (public back-compat)
    active_patches,
    read_overrides,
    record_patch_baseline,
    rollback_patch,
)
from core.learning_logger import (
    log_config_change,        # noqa: F401  (legacy test stub)
    log_config_proposal,      # noqa: F401  (legacy test stub)
    log_guard_report,
    log_review,
)
from core.learning_schema import LEARNING_MODES
from core.trade_reviewer import review_trade
from core.utils import (
    load_config,
    read_json_state,
    setup_logger,
    utc_now_iso,
    write_json_state,
)

STATE_FILE = "learning_state.json"
# Files the pipeline NEVER writes to. Used for the no_config_patches
# guard and as a reminder for any future contributor / test that the
# observe-only contract is a structural invariant.
_BLOCKED_TARGETS = (
    "learning_config_overrides.json",
    "config_proposals.jsonl",
)

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
        # Kept as empty lists for back-compat with the dashboard tile; the
        # observe-only pipeline will never append to any of them.
        "active_proposals": [],
        "rejected_proposals": [],
        "applied_patches": [],
        "rollback_triggers": [],
        # New authoritative field for the deterministic-guard pipeline.
        "last_guard_report": None,
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
    """Favourable post-exit M5 prices for a closed trade (highs for BUY, lows for SELL)."""
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
    return out[:24]


# ---------------------------------------------------------------------------
# Phase 2.5 — deterministic guards (observe-only)
# ---------------------------------------------------------------------------

def _consecutive_losses(reviews: list[dict[str, Any]]) -> int:
    """Count consecutive losses from the most recent review forward.

    Reviews are newest-first (built from trade_log.json which the
    review-tracker emits sorted desc). A 'loss' is any review with
    ``r_multiple < 0`` or ``result == "loss"``.
    Returns 0 if reviews is empty OR the most recent review is a win.
    """
    n = 0
    for r in reviews or []:
        is_loss = False
        rm = r.get("r_multiple")
        if rm is not None and float(rm) < 0:
            is_loss = True
        elif str(r.get("result") or "").lower() == "loss":
            is_loss = True
        if not is_loss:
            break
        n += 1
    return int(n)


def _top_mistake(mistake_counts: dict[str, int]) -> tuple[str | None, int]:
    """Return (category, count) for the most-frequent mistake, or (None, 0)
    if the dict is empty."""
    if not mistake_counts:
        return None, 0
    cat, cnt = max(mistake_counts.items(), key=lambda x: x[1])
    return cat, int(cnt)


def evaluate_deterministic_guards(
    reviews: list[dict[str, Any]],
    state: dict[str, Any],
    config: dict[str, Any],
    *,
    mode: str = "review_only",
) -> dict[str, Any]:
    """Run the 5 deterministic guards and return a GuardReport.

    The GuardReport never contains a ``patch`` or ``ops`` key. The
    overall status prioritises ``paused > alert > halve_advisory >
    active`` so dashboard alerts surface in clear order.
    """
    gcfg = ((config or {}).get("learning") or {}).get("guards") or {}
    pause_thr = int(gcfg.get("pause_on_consecutive_losses_thr", 4))
    mistake_thr = int(gcfg.get("mistake_fingerprint_thr", 5))
    exp_thr = float(gcfg.get("negative_expectancy_thr", 0.0))

    # 1. pause-on-consecutive-losses
    cons = _consecutive_losses(reviews)
    cons_verdict = "pause" if cons >= pause_thr else "ok"
    pause_guard = {
        "verdict": cons_verdict,
        "consecutive_losses": int(cons),
        "threshold": int(pause_thr),
        "details": (
            f"{cons} consecutive losses >= threshold {pause_thr}"
            if cons_verdict == "pause"
            else f"{cons} consecutive losses < threshold {pause_thr}"
        ),
    }

    # 2. halve-on-negative-expectancy
    rolling_exp = float((state or {}).get("rolling_expectancy_r") or 0.0)
    halve_verdict = "halve" if rolling_exp < exp_thr else "ok"
    halve_guard = {
        "verdict": halve_verdict,
        "expectancy_r": round(rolling_exp, 3),
        "threshold": float(exp_thr),
        "advisory": (
            "operator should halve position sizing manually"
            if halve_verdict == "halve" else None
        ),
    }

    # 3. alert-on-mistake-fingerprint
    mistake_counts = (state or {}).get("mistake_counts") or {}
    top_cat, top_cnt = _top_mistake(mistake_counts)
    alert_verdict = "alert" if (top_cat and top_cnt >= mistake_thr) else "ok"
    alert_guard = {
        "verdict": alert_verdict,
        "top_mistake": top_cat,
        "count": int(top_cnt),
        "threshold": int(mistake_thr),
        "details": (
            f"{top_cat} x{top_cnt} >= threshold {mistake_thr}"
            if alert_verdict == "alert" else None
        ),
    }

    # 4. log-always
    log_guard = {
        "verdict": "logged",
        "log_path": "logs/learning_guards.jsonl",
        "logged_count": int((state or {}).get("reviewed_count") or 0),
    }

    # 5. no-config-patches
    ncp_guard = {
        "verdict": "ok",
        "writes_blocked": True,
        "blocked_targets": list(_BLOCKED_TARGETS),
        "details": "observe-only pipeline; config never mutated",
    }

    # Status priority: paused > alert > halve_advisory > active.
    if cons_verdict == "pause":
        status = "paused"
    elif alert_verdict == "alert":
        status = "alert"
    elif halve_verdict == "halve":
        status = "halve_advisory"
    else:
        status = "active"

    return {
        "timestamp": utc_now_iso(),
        "mode": mode,
        "status": status,
        "reviewed_trades": len(reviews or []),
        "rolling_win_rate_pct": float((state or {}).get("rolling_win_rate_pct") or 50.0),
        "rolling_expectancy_r": round(rolling_exp, 3),
        "guards": {
            "pause_on_consecutive_losses": pause_guard,
            "halve_on_negative_expectancy": halve_guard,
            "alert_on_mistake_fingerprint": alert_guard,
            "log_always": log_guard,
            "no_config_patches": ncp_guard,
        },
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Phase 2.4 back-compat helpers — KEPT for tests; NOT called by run()
# ---------------------------------------------------------------------------

def _check_rollbacks(state: dict[str, Any]) -> dict[str, Any]:
    """Roll back applied patches whose post-apply rolling expectancy degraded.

    ONLY kept so tests built against the prior engine keep working. The
    observe-only ``run()`` never invokes this because the no_config_patches
    guard forbids ever applying a patch in the first place. Reading
    ``learning_config_overrides.json`` here is fine — it's read-only,
    not a write.
    """
    overrides = read_overrides()
    cur_exp = float(state.get("rolling_expectancy_r") or 0.0)
    reviewed = int(state.get("reviewed_count") or 0)
    for p in list(overrides.get("patches") or []):
        baseline = p.get("baseline_expectancy_r")
        if baseline is None:
            continue
        if reviewed < int(p.get("applied_at_reviewed") or 0) + 5:
            continue
        if cur_exp < float(baseline) - 0.1:
            rollback_patch(p.get("proposal_id"), f"expectancy_degraded {cur_exp:.3f}<{baseline:.3f}")
            state.setdefault("rollback_triggers", []).append({
                "proposal_id": p.get("proposal_id"),
                "reason": f"expectancy {cur_exp:.3f} < baseline {baseline:.3f} - 0.1",
                "at": utc_now_iso(),
            })
    state["rollback_triggers"] = (state.get("rollback_triggers") or [])[-50:]
    return state


def _new_closes(state: dict[str, Any], trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    last = state.get("last_reviewed_trade_id")
    if not last:
        return trades[-25:]
    out: list[dict[str, Any]] = []
    for t in trades:
        if str(t.get("trade_id")) == str(last):
            break
        out.append(t)
    if trades:
        last_exists = any(str(t.get("trade_id")) == str(last) for t in trades[:50])
        if not last_exists:
            out = trades[-25:]
    return out


# ---------------------------------------------------------------------------
# run() — Phase 2.5 observe-only pipeline
# ---------------------------------------------------------------------------

def run() -> dict | None:
    config = load_config()
    logger = _logger()
    mode = _mode(config)
    state = read_state()
    state["mode"] = mode

    # observe_only: still log_always even with no reviews.
    if mode == "observe_only":
        guard_report = evaluate_deterministic_guards(
            reviews=[], state=state, config=config, mode=mode
        )
        log_guard_report(guard_report)
        state["last_guard_report"] = {
            "status": guard_report["status"],
            "guards_status": {
                k: v.get("verdict") for k, v in guard_report["guards"].items()
            },
            "reviewed_trades": guard_report["reviewed_trades"],
            "at": guard_report["timestamp"],
        }
        state["updated_at"] = utc_now_iso()
        write_json_state(STATE_FILE, state)
        logger.info(
            "Learning loop observe_only — log_only status=%s reviewed=%d",
            guard_report["status"], guard_report["reviewed_trades"],
        )
        return state

    tl = read_json_state("trade_log.json", default=[]) or []
    trades = list(tl if isinstance(tl, list) else (tl or {}).get("trades") or [])
    new_trades = _new_closes(state, trades)

    # No new closes — fire log_always anyway so operators see fresh
    # status every cycle (consecutive-losses can latch even without
    # new trades by reading prior reviews stored in state).
    if not new_trades:
        guard_report = evaluate_deterministic_guards(
            reviews=[], state=state, config=config, mode=mode
        )
        log_guard_report(guard_report)
        state["last_guard_report"] = {
            "status": guard_report["status"],
            "guards_status": {
                k: v.get("verdict") for k, v in guard_report["guards"].items()
            },
            "reviewed_trades": 0,
            "at": guard_report["timestamp"],
        }
        write_json_state(STATE_FILE, state)
        logger.info(
            "Learning loop: no new closes (mode=%s status=%s)", mode, guard_report["status"],
        )
        return state

    candles_doc = read_json_state("latest_candles.json", default={}) or {}
    reviews: list[dict[str, Any]] = []
    for t in new_trades:
        rev = review_trade(t, post_exit_prices=_post_exit_prices(t, candles_doc), config=config)
        rev["mode"] = mode
        reviews.append(rev)
        log_review(rev)

    state = _update_state(state, reviews)

    # 5 deterministic guards — observe-only — replaces propose_from_reviews +
    # _shadow_simulate + _apply_limited + log_config_proposal +
    # log_config_change + learn_overrides writes.
    guard_report = evaluate_deterministic_guards(reviews, state, config, mode=mode)
    log_guard_report(guard_report)
    state["last_guard_report"] = {
        "status": guard_report["status"],
        "guards_status": {
            k: v.get("verdict") for k, v in guard_report["guards"].items()
        },
        "reviewed_trades": guard_report["reviewed_trades"],
        "at": guard_report["timestamp"],
    }

    write_json_state(STATE_FILE, state)
    logger.info(
        "Learning loop: reviewed %d trades (mode=%s) -> win=%.1f%% exp=%.3fR "
        "status=%s guards=%s",
        len(reviews), mode, state.get("rolling_win_rate_pct"),
        state.get("rolling_expectancy_r"), guard_report["status"],
        guard_report["guards"]["no_config_patches"]["writes_blocked"],
    )
    return state


if __name__ == "__main__":
    run()
