"""Payoff Paradox Meter (2026-07-20).

A small, pure helper module that powers the live dashboard tile on the Profit
Quality card. The meter has four knobs:

* ``incremental_WR`` -- win rate over the *review window* (the trades that
  have closed since the last apply of a payoff_paradox proposal; falls back
  to the last 30 closes if no proposal is on record yet).
* ``payoff``          -- ratio of gross-win to gross-loss within the window;
  ``float('inf')`` if there were wins but no losses, ``0.0`` when the window
  has no closed trades at all yet.
* ``#BE-floor-suppressed`` -- count of mgmt_winners inside the window whose
  realised_r sits *below* the proposed new floor, i.e. the BE lock the bot
  would otherwise fire fire-fired before the trade realised further upside.
  Proxy-only today (no per-trade `_be_suppressed` flag yet); the count is
  reproducible from realised_r alone.
* ``suggested_next_floor`` -- {0.4 | 0.5 | 0.6}; computed by the same
  three-bucket median-winner-R rule as
  scripts/fit_payoff_paradox_floor.py::floor_for_median_r. Ratchet-only:
  any value < current never appears in the suggestion.

The module also bundles ``write_payoff_paradox_proposal(...)`` -- a
de-bounced writer that turns the meter into a Phase 2.4 bounded proposal
in state/learning_config_overrides.json. Core/learning_overrides.py picks
the patch up on the next config load; live_apply_limited gates apply auto
so the operator always sees the suggestion in the dashboard first via
the meter tile.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

import yaml  # noqa: F401  (kept for symmetry; not directly used yet)

from core.utils import (
    PROJECT_ROOT,
    STATE_DIR,
    utc_now_iso,
)


# Mirror the BOUNDED_TUNABLES tuple for trading.exits.min_r_multiple_win so
# the meter doesn't import core.config_proposal directly (avoid circular
# import in tests that monkeypatch the BOUNDED_TUNABLES dict). Also keep
# this as the single source of truth for the ratchet cap and tunables path.
METER_TUNABLE_PATH: tuple[str, ...] = ("trading", "exits", "min_r_multiple_win")
# (delta_min, delta_max). ratchet-only: min=0.0 (no demote); max=0.1 (single
# step). Matches core/config_proposal.BOUNDED_TUNABLES so the proposal
# passes the existing is_dangerous / _clamp_delta gates.
METER_DELTA_BOUNDS: tuple[float, float] = (0.0, 0.1)
CANDIDATE_FLOORS: tuple[float, ...] = (0.4, 0.5, 0.6)
# Operators need review before the patch is auto-applied. live_apply_limited
# honours this flag; observe-only / shadow_apply ignore it.
PROPOSAL_AUTO_APPLY_ALLOWED: bool = False

REVIEW_WINDOW_DEFAULT: int = 30
# Cooldown between proposal writes for the same tunable. Operators want to
# see the meter update every 5s but not have the override JSON churn on
# every bot cycle. Floor at 5 minutes.
PROPOSAL_COOLDOWN_SECONDS: int = 5 * 60
# Maximum entries retained in `suppressed_realised_rs` (kept in the SSE
# payload). Caps bandwidth on a 5s cadence even when a debug session widens
# review_window to thousands. Full count is in BE_floor_suppressed_count.
SUPPRESSED_RS_CAP: int = 10

# Suggestion-text templates — operator-facing, not data.
_SUGGESTION_NO_CHANGE = "Floor {current} unchanged — review window shows median_winner_r={median:.2f}R on {n} closes."
_SUGGESTION_RATCHET = (
    "{n} winners below {target}R in the review window; "
    "consider ratcheting floor from {current} -> {target} next session."
)
_SUGGESTION_INSUFFICIENT = (
    "Review window too small ({n} closes); holding current floor {current}."
)
# Same ratchet suggestion text but flagged "deferred" so the operator can
# distinguish "in-flight signal, auto-write next-cycle" from "would-do-but-gated".
_SUGGESTION_RATCHET_DEFERRED = (
    "{n} winners below {target}R; consider ratcheting {current} -> {target} next "
    "session, but defer (only {n_window}/{review_window} closes yet)."
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _realised_r(trade: dict[str, Any]) -> float | None:
    """Compute realised_r for a closed trade. Returns None if entry/sl/size
    are unintelligible (the trade was unpriced at close)."""
    entry = _safe_float(trade.get("entry"))
    sl = _safe_float(trade.get("sl"))
    pnl = _safe_float(trade.get("pnl"))
    size = _safe_float(trade.get("size"))
    if entry is None or sl is None or sl <= 0 or pnl is None or size is None or size <= 0:
        return None
    return (pnl / size) / abs(entry - sl)


def _is_mgmt_winner(trade: dict[str, Any]) -> bool:
    """A trade where position management intervened (BE locked, partial-TP
    taken, or trailing active). Detected from the existing per-trade
    fields so we don't need a new flag in trade_log.json."""
    be = bool(trade.get("be_triggered"))
    ptp = bool(trade.get("partial_tp_done"))
    trail = bool(trade.get("trail_active"))
    pnl = _safe_float(trade.get("pnl")) or 0.0
    return pnl > 0 and (be or ptp or trail)


def floor_for_median_r(median_r: float, candidates: tuple[float, ...] = CANDIDATE_FLOORS) -> float:
    """Three-bucket heuristic. Same logic as
    scripts/fit_payoff_paradox_floor.py::floor_for_median_r — mirrored here
    so the meter doesn't import the script (cheap import; small wrapper).
    Always ratchets (returns ``candidates[0]`` for non-positive medians).
    """
    if not isinstance(median_r, (int, float)) or median_r <= 0:
        return candidates[0]
    if median_r > 0.65:
        return 0.6
    if median_r > 0.45:
        return 0.5
    return 0.4


def ratchet_up_only(proposed: float, current: float, candidates: tuple[float, ...] = CANDIDATE_FLOORS) -> float:
    """Never step DOWN. If the fitter suggests a value below current_floor,
    return ``current_floor`` instead. If the value isn't in candidates,
    snap to the nearest candidate ABOVE current (else current)."""
    if proposed < current:
        return current
    if proposed in candidates:
        return float(proposed)
    above = sorted(c for c in candidates if c >= current)
    if not above:
        return current
    # Nearest of the above-candidates to proposed (ratchet-friendly)
    return min(above, key=lambda c: abs(float(c) - float(proposed)))


# ---------------------------------------------------------------------------
# Meter computation
# ---------------------------------------------------------------------------


def compute_payoff_paradox_meter(
    trades: list[dict[str, Any]],
    *,
    current_floor: float,
    review_window: int = REVIEW_WINDOW_DEFAULT,
    candidates: tuple[float, ...] = CANDIDATE_FLOORS,
) -> dict[str, Any]:
    """Inspect the LAST ``review_window`` trades, compute 4 scalars, and
    return a meter dict. Functions and code never reads files: callers pass
    the trade list in directly so this is trivially testable."""
    window_trades = trades[-review_window:] if review_window and trades else []
    n_window = len(window_trades)

    winners = [t for t in window_trades if (_safe_float(t.get("pnl")) or 0.0) > 0]
    losers = [t for t in window_trades if (_safe_float(t.get("pnl")) or 0.0) <= 0]

    n_w = len(winners)
    n_l = len(losers)

    # incremental_WR: percentage 0.0-100.0; 0.0 when no window yet.
    incremental_wr = round((n_w / n_window) * 100.0, 2) if n_window else 0.0

    gross_win = sum(_safe_float(t.get("pnl")) or 0.0 for t in winners)
    gross_loss = abs(sum(_safe_float(t.get("pnl")) or 0.0 for t in losers))  # abs(loss)

    # Empty window: payoff is undefined. Return 0.0 (not inf) so the JSON
    # serialiser doesn't reject and the UI shows "0.00" instead of "∞".
    if n_window == 0:
        payoff: float = 0.0
    elif n_w > 0 and gross_loss == 0.0:
        payoff = float("inf")
    elif n_l > 0 and gross_win == 0.0:
        payoff = 0.0
    else:
        payoff = round(gross_win / gross_loss, 3) if gross_loss else float("inf")

    # #BE-floor-suppressed: count of mgmt_winners where realised_r < new_floor.
    # Since current_floor IS new_floor in the meter context (we're not
    # re-fitting — that's fit_payoff_paradox_floor's job), the count is of
    # mgmt_winners strictly below current_floor. Cheap proxy for "BE moved
    # before the trade could extend" without needing the per-trade flag.
    suppressed_count = 0
    suppressed_rs: list[float] = []
    for t in winners:
        if not _is_mgmt_winner(t):
            continue
        rr = _realised_r(t)
        if rr is None or rr >= current_floor:
            continue
        # At-or-below floor winners with mgmt intervention are the ones
        # whose BE lock prevented further upside.
        suppressed_count += 1
        suppressed_rs.append(rr)

    # Compute suggested_next_floor from realised winners' median R.
    realised_winners = sorted(filter(None, (_realised_r(t) for t in winners)))
    median_r = statistics.median(realised_winners) if realised_winners else 0.0
    proposed = floor_for_median_r(median_r, candidates)
    suggested = ratchet_up_only(proposed, current_floor, candidates)

    # Priced-SL coverage diagnostic (2026-07-20 MED reviewer #1): the
    # fitter's signal hinges on realised_r for WINNERS specifically, so we
    # split coverage into two: priced_sl_coverage_pct (all trades) and
    # priced_sl_winner_coverage_pct (winners only). The tile can then
    # distinguish "universally blind" (paper broker) from "winners blind
    # but losers priced" — which the operator wants to see when ALL losers
    # carry SLs but the bot quietly regresses on winners.
    def _has_priced_sl(t: dict[str, Any]) -> bool:
        return (
            _safe_float(t.get("entry")) is not None
            and _safe_float(t.get("sl")) is not None
            and _safe_float(t.get("sl")) > 0
            and _safe_float(t.get("size")) is not None
            and _safe_float(t.get("size")) > 0
        )

    if n_window == 0:
        priced_sl_coverage_pct = 0.0
        priced_sl_winner_coverage_pct = 0.0
    else:
        priced_all = sum(1 for t in window_trades if _has_priced_sl(t))
        priced_winners = sum(1 for t in winners if _has_priced_sl(t))
        priced_sl_coverage_pct = round(100.0 * priced_all / n_window, 2)
        priced_sl_winner_coverage_pct = (
            round(100.0 * priced_winners / n_w, 2) if n_w else 0.0
        )

    # Operator-facing suggestion text. The `deferred` flag controls whether
    # the suggestion is shown as a pending proposal-vs-observation. The
    # auto-write side (write_payoff_paradox_proposal) independently blocks on
    # n_window< review_window via its own gate.
    delta = round(suggested - current_floor, 4)
    deferred = int(n_window) < int(review_window)
    target_below_n = sum(
        1 for t in winners
        if _is_mgmt_winner(t)
        and _realised_r(t) is not None
        and _realised_r(t) < suggested
    )
    if delta < 1e-9:
        if deferred:
            suggestion_text = _SUGGESTION_INSUFFICIENT.format(n=n_window, current=current_floor)
        else:
            suggestion_text = _SUGGESTION_NO_CHANGE.format(
                current=current_floor, median=median_r, n=n_window,
            )
    else:
        if deferred:
            suggestion_text = _SUGGESTION_RATCHET_DEFERRED.format(
                n=target_below_n, target=suggested, current=current_floor,
                n_window=n_window, review_window=review_window,
            )
        else:
            suggestion_text = _SUGGESTION_RATCHET.format(
                n=target_below_n, target=suggested, current=current_floor,
            )
    # Public flag so JS / supervisors can theme the tile differently for
    # "deferred" vs "live ready" suggestion state without re-parsing text.
    suggestion_deferred = bool(deferred and delta > 0)

    return {
        "incremental_WR": incremental_wr,
        "payoff": payoff,
        "BE_floor_suppressed_count": suppressed_count,
        "suppressed_realised_rs": [round(r, 4) for r in suppressed_rs[-SUPPRESSED_RS_CAP:]],
        "suggested_next_floor": float(suggested),
        "suggested_delta": delta,
        "suggestion_text": suggestion_text,
        "suggestion_deferred": suggestion_deferred,
        "median_winner_r": round(median_r, 6),
        "n_window": n_window,
        "n_winners": n_w,
        "n_losers": n_l,
        "current_floor": float(current_floor),
        "review_window": int(review_window),
        "ts": utc_now_iso(),
        "priced_sl_coverage_pct": priced_sl_coverage_pct,
        "priced_sl_winner_coverage_pct": priced_sl_winner_coverage_pct,
        "suppressed_truncated": len(suppressed_rs) > SUPPRESSED_RS_CAP,
    }


# ---------------------------------------------------------------------------
# State file plumbing
# ---------------------------------------------------------------------------


def _state_overrides_path(filename: str = "learning_config_overrides.json") -> Path:
    return STATE_DIR / filename


def _read_existing_proposals(filename: str = "learning_config_overrides.json") -> dict[str, Any]:
    """Read state/learning_config_overrides.json. Returns the parsed obj
    (with 'patches' + 'rollbacks' keys) or a fresh skeleton if the file
    is missing/malformed — the file is owner-written (the bot only reads).
    """
    path = _state_overrides_path(filename)
    if not path.exists():
        return {"patches": [], "rollbacks": []}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.loads(fh.read() or "{}")
        if not isinstance(data, dict):
            return {"patches": [], "rollbacks": []}
        data.setdefault("patches", [])
        data.setdefault("rollbacks", [])
        return data
    except (OSError, json.JSONDecodeError):
        return {"patches": [], "rollbacks": []}


def last_proposal_ts_for_tunable(
    tunable_path: tuple[str, ...], filename: str = "learning_config_overrides.json"
) -> float | None:
    """Return the unix-time of the most-recent unapplied proposal for the
    given tunable, or None if none exists. Used to enforce the cooldown."""
    doc = _read_existing_proposals(filename)
    candidates: list[float] = []
    for patch in doc.get("patches") or []:
        meta_path = (patch.get("patch") or {}).get("path")
        if not isinstance(meta_path, list) or tuple(meta_path) != tunable_path:
            continue
        ts = patch.get("ts")
        try:
            if ts is not None:
                candidates.append(float(ts))
        except (TypeError, ValueError):
            continue
    return max(candidates) if candidates else None


def write_payoff_paradox_proposal(
    meter: dict[str, Any],
    *,
    current_floor: float,
    tunable_path: tuple[str, ...] = METER_TUNABLE_PATH,
    delta_bounds: tuple[float, float] = METER_DELTA_BOUNDS,
    cooldown_seconds: int = PROPOSAL_COOLDOWN_SECONDS,
    now: float | None = None,
    filename: str = "learning_config_overrides.json",
) -> dict[str, Any] | None:
    """Write a bounded-proposal patch into state/learning_config_overrides.json
    IF conditions are met (ratchet-only, past cooldown, suggestion differs).
    Behaviour:

    * If ``suggested_next_floor == current_floor``: de-bounce the meter by
      **removing** any stale payoff_paradox patches so the operator's view
      of pending proposals stays clean.
    * If the suggestion is a ratchet AND the cooldown has elapsed AND there
      are enough review-window closes (>= review_window): append a new
      patch with ``auto_apply_allowed=False`` (operator must approve).
    * Otherwise: no-op, return None.

    Returns the patch dict that was written (or the cleared-tombstone
    record), or None when nothing changed.
    """
    now_ts = float(now) if now is not None else time.time()
    suggested = float(meter.get("suggested_next_floor") or current_floor)
    current = float(current_floor)
    delta = round(suggested - current, 4)

    if delta < 1e-9:
        # Stale-ratchet clear: drop any prior payoff_paradox patches so the
        # operator's UI doesn't show "consider ratcheting" when the meter
        # has just rolled back to current=hold.
        doc = _read_existing_proposals(filename)
        kept_patches = [
            p for p in (doc.get("patches") or [])
            if not (isinstance((p.get("patch") or {}).get("path"), list)
                    and tuple((p.get("patch") or {}).get("path")) == tunable_path)
        ]
        if len(kept_patches) != len(doc.get("patches") or []):
            doc["patches"] = kept_patches
            _persist_overrides(doc, filename)
        return None

    # Ratchet validity check — same as ratchet_up_only, restated explicitly.
    if suggested < current:
        return None

    # Bound check at the patch level (defence in depth, in addition to
    # core.config_proposal.is_dangerous() which is called by load_config()).
    if delta < delta_bounds[0] - 1e-9 or delta > delta_bounds[1] + 1e-9:
        return None

    # Window-size guard.
    if int(meter.get("n_window") or 0) < int(meter.get("review_window") or REVIEW_WINDOW_DEFAULT):
        return None

    # Cooldown — at most one proposal per tunable per cooldown_seconds.
    last_ts = last_proposal_ts_for_tunable(tunable_path, filename)
    if last_ts is not None and (now_ts - last_ts) < cooldown_seconds:
        return None

    # Idempotent overwrite: if a same-tunable patch with the same suggested
    # value already exists, just bump its ts.
    doc = _read_existing_proposals(filename)
    proposed_value = round(current + delta_bounds[0] + delta, 6)  # = current + delta
    for existing in doc.get("patches") or []:
        ep = (existing.get("patch") or {})
        if isinstance(ep.get("path"), list) and tuple(ep["path"]) == tunable_path:
            if round(float(ep.get("value", 0)), 4) == round(proposed_value, 4):
                existing["ts"] = now_ts
                existing.setdefault("evidence", {})
                # Keep meter_revisions history AND update top-level fields so
                # the operator's view sees the latest snapshot without losing
                # the initial signal. The dashboard reads top-level evidence
                # directly; meter_revisions is the audit trail (capped at 10).
                meter_rev = {
                    "incremental_WR": meter.get("incremental_WR"),
                    "payoff": meter.get("payoff"),
                    "BE_floor_suppressed_count": meter.get("BE_floor_suppressed_count"),
                    "median_winner_r": meter.get("median_winner_r"),
                    "n_window": meter.get("n_window"),
                }
                history = existing["evidence"].get("meter_revisions") or []
                history.append(meter_rev)
                existing["evidence"]["meter_revisions"] = history[-10:]
                existing["evidence"].update(meter_rev)
                _persist_overrides(doc, filename)
                return existing

    patch = {
        "proposal_id": (
            f"payoff_paradox_floor_{tunable_path[-1]}_"
            f"{round(proposed_value, 4)}_{int(now_ts)}"
        ),
        "ts": now_ts,
        "patch": {
            "path": list(tunable_path),
            "value": round(proposed_value, 6),
            "delta_bounds": list(delta_bounds),
        },
        "evidence": {
            "metric": "payoff_paradox_meter",
            "suggestion_text": meter.get("suggestion_text"),
            "incremental_WR": meter.get("incremental_WR"),
            "payoff": meter.get("payoff"),
            "BE_floor_suppressed_count": meter.get("BE_floor_suppressed_count"),
            "median_winner_r": meter.get("median_winner_r"),
            "n_window": meter.get("n_window"),
            "n_winners": meter.get("n_winners"),
            "n_losers": meter.get("n_losers"),
            "current_floor": current,
            "suggested_delta": delta,
        },
        "risk_level": "low",
        "auto_apply_allowed": PROPOSAL_AUTO_APPLY_ALLOWED,
        "status": "pending",
    }
    doc["patches"].append(patch)
    _persist_overrides(doc, filename)
    return patch


def _persist_overrides(doc: dict[str, Any], filename: str) -> None:
    """Atomic writeback to state/learning_config_overrides.json so a
    bot-cycle reading the file mid-write doesn't see a half-serialised
    payload. tmp + os.replace preserves the read-after-write invariant."""
    path = _state_overrides_path(filename)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        logging.getLogger("payoff_paradox_meter").warning(
            "Could not persist overrides to %s: %s", path, exc,
        )
