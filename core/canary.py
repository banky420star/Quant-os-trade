"""Symbol canary A/B state machine.

Picks the highest-projection-AND-trusted symbol each morning and gives
it ``canary.lot_multiplier`` (default 1.5x) entry volume as an A/B
experiment vs a quiet ``canary.control_symbol`` (default XAUUSDm).

Every cycle produces a ``canary_audit.jsonl`` row capturing the decision.
After 7 cycles the promotion function checks whether the canary
outperformed the control by > ``projected_daily * 0.5``; if so the canary
persists (state-machine status=``promoted``), otherwise it demotes
(status=``demoted``). Alpha decay is detected when the realised daily
PnL diverges > 1 sigma from projection for 2 consecutive weekly windows;
on decay the canary demotes early (status=``decayed``) — still no config
mutation, just an audit entry.

What this module DOES NOT do
=============================
- Does NOT trade live or paper.
- Does NOT write to ``config.yaml``, ``learning_config_overrides.json``
  or ``config_proposals.jsonl``. The canary is observe-only by design.
- Does NOT mutate any source-code rule. The ``canary_state.json`` file
  is the only persistent state, plus an append-only JSONL audit.

Operator veto
=============
To disable the canary immediately, delete ``state/canary_state.json`` and
the next ``run_canary_pick.py`` invocation will pick a fresh one (or
none). To pin a specific symbol, set ``config.canary.pinned_symbol`` to
that symbol's name (or set to empty string to disable the canary
entirely).
"""

from __future__ import annotations

import json
import logging
import random
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.utils import STATE_DIR, utc_now_iso, write_json_state

CANARY_STATE_FILE = "canary_state.json"
CANARY_AUDIT_FILE = "canary_audit.jsonl"

DEFAULT_CONTROL_SYMBOL = "XAUUSDm"
DEFAULT_LOT_MULTIPLIER = 1.5
DEFAULT_PROMOTION_HALF_PROJECTION = 0.5
DEFAULT_PROMOTION_WINDOW_DAYS = 7
DEFAULT_ALPHA_DECAY_WINDOW_DAYS = 14
DEFAULT_ALPHA_DECAY_SIGMA = 1.0
DEFAULT_MIN_N_PER_SYMBOL = 30  # need this many closed trades per symbol for any symbol to be trusted input

# Status enum
STATUS_INIT = "init"
STATUS_PICKED = "picked"
STATUS_PROMOTED = "promoted"
STATUS_DEMOTED = "demoted"
STATUS_DECAYED = "decayed"
STATUS_DISABLED = "disabled"
STATUS_NO_TRUSTED = "no_trusted"


# ----------------------------------------------------------------------
# State dataclass
# ----------------------------------------------------------------------
@dataclass
class CanaryState:
    status: str = STATUS_INIT
    pinned_symbol: str | None = None
    symbol: str | None = None
    control_symbol: str = DEFAULT_CONTROL_SYMBOL
    lot_multiplier: float = DEFAULT_LOT_MULTIPLIER
    picked_at: str | None = None
    picked_reason: str = ""
    promoted_at: str | None = None
    demoted_at: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    last_audit_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CanaryState":
        # Backward-compatible: unknown keys are dropped, missing keys use defaults.
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in allowed})


# ----------------------------------------------------------------------
# Pure helpers — no I/O
# ----------------------------------------------------------------------
def pick_canary(
    projection_rows: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    *,
    min_trusted_n: int = DEFAULT_MIN_N_PER_SYMBOL,
) -> dict[str, Any] | None:
    """Pick the highest-projection-AND-trusted symbol from verify_edge.

    Trust gate is: ``row['trusted'] is True AND row['n'] >= min_trusted_n``.
    The symbol with the highest ``best.projected_daily_pnl_usd`` wins.
    Returns a pick dict or ``None`` if no trusted row is available.

    Pure function — same inputs ⇒ same output (modulo ``pinned_symbol``).
    """
    if not projection_rows:
        return None
    cfg = config or {}
    pinned = (cfg.get("canary") or {}).get("pinned_symbol", None)
    if pinned == "":
        return None  # operator-vetoed
    trusted_rows = [
        r for r in projection_rows
        if r.get("trusted") is True
        and int(r.get("n", 0)) >= int(min_trusted_n)
    ]
    if pinned:
        # Pin → always pick that symbol if it is trusted enough.
        pin_row = next(
            (r for r in trusted_rows if r.get("symbol") == pinned),
            None,
        )
        if pin_row is None:
            return None
        return {
            "symbol": pin_row.get("symbol"),
            "expected_daily_usd": float(pin_row.get("best", {}).get("projected_daily_pnl_usd", 0.0) or 0.0),
            "n": int(pin_row.get("n", 0)),
            "expectancy_r": float(pin_row.get("best", {}).get("expectancy_r", 0.0) or 0.0),
            "ci95_daily": pin_row.get("best", {}).get("ci95_daily"),
            "pinned": True,
            "reason": "pinned_by_operator",
        }
    if not trusted_rows:
        return None
    ranked = sorted(
        trusted_rows,
        key=lambda r: float(r.get("best", {}).get("projected_daily_pnl_usd", 0.0) or 0.0),
        reverse=True,
    )
    winner = ranked[0]
    return {
        "symbol": winner.get("symbol"),
        "expected_daily_usd": float(winner.get("best", {}).get("projected_daily_pnl_usd", 0.0) or 0.0),
        "n": int(winner.get("n", 0)),
        "expectancy_r": float(winner.get("best", {}).get("expectancy_r", 0.0) or 0.0),
        "ci95_daily": winner.get("best", {}).get("ci95_daily"),
        "pinned": False,
        "reason": "highest_trusted_projection",
    }


def _parse_iso_date(ca: Any) -> str | None:
    """Strictly validate a `closed_at` timestamp and return the UTC date key.

    Returns None for any non-ISO 8601 input so the trade is dropped from
    realised-window calculations rather than silently bucketed on ``[:10]``
    of a Unix timestamp or other malformed string.
    """
    if not ca:
        return None
    s = str(ca).strip()
    if len(s) < 10:
        return None
    # Must start with an ISO date YYYY-MM-DD
    date_part = s[:10]
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
    except ValueError:
        return None
    return date_part


def _bucket_realized_by_day(trades: list[dict[str, Any]]) -> dict[str, float]:
    """Sum realized PnL per UTC day keyed by ``YYYY-MM-DD``.

    2026-07-22: hardened — only accepts ISO 8601 ``closed_at`` /
    ``exit_time`` strings; malformed timestamps are dropped instead of
    bucketed silently. This stops the realised-window from catching a
    Unix timestamp's literal first 10 characters as a bogus date.
    """
    out: dict[str, float] = {}
    for t in trades or []:
        ca = t.get("closed_at") or t.get("exit_time") or ""
        sym = t.get("symbol")
        date = _parse_iso_date(ca)
        if date is None or sym is None:
            continue
        try:
            pnl = float(t.get("pnl") or t.get("profit") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        out.setdefault(date, 0.0)
        out[date] += pnl
    return out


def _days_since_iso(ts_iso: str | None) -> int:
    """Compute whole UTC days elapsed since `ts_iso` (or 0 on parse fail)."""
    if not ts_iso:
        return 0
    try:
        dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0
    return (datetime.now(timezone.utc) - dt).days


def _window_has_real_data(window: list[float]) -> bool:
    """True when at least one entry in the window is non-zero."""

    return any(abs(x) > 1e-9 for x in window)


def _realized_daily_series(
    trades: list[dict[str, Any]],
    symbol: str,
    days: int,
) -> list[float]:
    """Return a chronologically-ascending list of realised PnL per day for `symbol`
    over the last ``days`` days (zero for days with no closes).
    """
    if days <= 0:
        return []
    sym_trades = [t for t in (trades or []) if t.get("symbol") == symbol]
    daily = _bucket_realized_by_day(sym_trades)
    # Use the most recent `days` distinct dates that have either trade data
    # OR fall back to a window anchored on today.
    today = datetime.now(timezone.utc).date()
    series: list[float] = []
    for i in range(days - 1, -1, -1):
        d = (today.toordinal() - i)
        date_str = datetime.fromordinal(d).date().isoformat()
        series.append(daily.get(date_str, 0.0))
    return series


def _today_iso_date(now_iso: str | None = None) -> str:
    """Return the YYYY-MM-DD UTC date stamp used for idempotence."""
    if now_iso:
        return now_iso[:10]
    return datetime.now(timezone.utc).date().isoformat()


def _upsert_today_history(history: list[dict[str, Any]], new_row: dict[str, Any]) -> str:
    """Idempotent: replace last row if its date == today, otherwise append.
    Returns 'overwrote' or 'appended'.
    """
    today = _today_iso_date(new_row.get("ts"))
    if history and str(history[-1].get("ts", ""))[:10] == today:
        history[-1] = new_row
        return "overwrote"
    history.append(new_row)
    return "appended"


def _mean(xs: list[float]) -> float:
    return float(sum(xs) / len(xs)) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return float(statistics.pstdev([x - m for x in xs]))


def _check_alpha_decay(
    realized_window: list[float],
    projection_daily_usd: float,
    *,
    sigma_threshold: float = DEFAULT_ALPHA_DECAY_SIGMA,
) -> tuple[bool, str]:
    """Return (decayed?, reason).

    Divergence: |mean_realized - projection_daily| > sigma_threshold * std_realized.
    Triggered when BOTH halves of a 2-week window each independently diverge.
    """
    n = len(realized_window)
    if n < 14 or projection_daily_usd == 0.0:
        return False, f"insufficient_data(n={n})"
    half = n // 2
    older, newer = realized_window[:half], realized_window[half:]

    def _diverges(half_window: list[float]) -> bool:
        if not half_window or projection_daily_usd == 0.0:
            return False
        m = _mean(half_window)
        s = _std(half_window)
        if s <= 1e-9:
            # Zero variance — converge mode means no decay possible.
            return False
        return abs(m - projection_daily_usd) > sigma_threshold * s

    if _diverges(older) and _diverges(newer):
        return True, "two_consecutive_weekly_windows_diverged"
    return False, "no_decay"


# ----------------------------------------------------------------------
# State-machine tick
# ----------------------------------------------------------------------
def tick(
    state: CanaryState,
    projection_rows: list[dict[str, Any]],
    *,
    realized_trades: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    log: logging.Logger | None = None,
) -> tuple[CanaryState, dict[str, Any]]:
    """Advance the canary state machine by one cycle. Pure.

    Inputs:
        state            — current CanaryState
        projection_rows  — full per-symbol rows from a verify_edge run
        realized_trades  — closed-trade history for realised-PnL lookback
        config           — ``config.yaml`` dict (controls thresholds + pin)

    Returns:
        (new_state, audit_row). The audit_row is a plain dict ready to
        be appended to ``canary_audit.jsonl``.
    """
    cfg = (config or {}).get("canary") or {}
    # enabled flag honored: when canary is disabled, no picks, no promos,
    # no lot multipliers; existing state is preserved (audit row only).
    enabled = bool(cfg.get("enabled", True))
    now = utc_now_iso()  # bound here so every early-return can use it safely
    if not enabled:
        state.status = STATUS_DISABLED
        state.last_audit_at = now
        return state, {
            "ts": now,
            "decision": "disabled_by_feature_flag",
            "symbol": state.symbol,
            "history_len": len(state.history),
            "promotion_check": {"eligible": False, "reason": "feature_off"},
            "alpha_decay": {"triggered": False, "reason": "feature_off"},
            "audit": {"enabled": False},
        }
    lot_mult = float(cfg.get("lot_multiplier", DEFAULT_LOT_MULTIPLIER))
    control_symbol = str(cfg.get("control_symbol", DEFAULT_CONTROL_SYMBOL))
    promoted_threshold = float(
        cfg.get("promotion_threshold_daily_pnl",
                DEFAULT_PROMOTION_HALF_PROJECTION)
    )
    promotion_window_days = int(
        cfg.get("promotion_window_days", DEFAULT_PROMOTION_WINDOW_DAYS)
    )
    decay_window_days = int(
        cfg.get("alpha_decay_window_days", DEFAULT_ALPHA_DECAY_WINDOW_DAYS)
    )
    sigma_threshold = float(
        cfg.get("alpha_decay_sigma", DEFAULT_ALPHA_DECAY_SIGMA)
    )

    realized_trades = list(realized_trades or [])
    pick = pick_canary(projection_rows, config)
    now = utc_now_iso()

    # --------------------------- Operator veto ---------------------------
    if cfg.get("pinned_symbol", "<UNSET>") == "":
        state.status = STATUS_DISABLED
        state.pinned_symbol = ""
        state.last_audit_at = now
        return state, {
            "ts": now,
            "decision": "disabled_by_operator",
            "symbol": state.symbol,
            "history_len": len(state.history),
            "promotion_check": {"eligible": False, "reason": "operator_veto"},
            "alpha_decay": {"triggered": False, "reason": "operator_veto"},
            "audit": {"pinned": ""},
        }

    # ---------------------- No trusted candidate ----------------------
    if pick is None:
        # Keep previous symbol if it already exists; just audit today's empty pick.
        state.status = STATUS_NO_TRUSTED if state.symbol is None else state.status
        state.last_audit_at = now
        return state, {
            "ts": now,
            "decision": "no_trusted_pick",
            "symbol": state.symbol,
            "history_len": len(state.history),
            "promotion_check": {"eligible": False, "reason": "no_trusted"},
            "alpha_decay": {"triggered": False, "reason": "no_trusted"},
            "audit": {
                "row_count": len(projection_rows or []),
                "trusted_count": sum(1 for r in (projection_rows or []) if r.get("trusted")),
            },
        }

    # ----------------------- First-time pick -----------------------
    if state.status == STATUS_INIT or state.symbol is None:
        state.status = STATUS_PICKED
        state.symbol = pick["symbol"]
        state.control_symbol = control_symbol
        state.lot_multiplier = lot_mult
        state.picked_at = now
        state.picked_reason = pick.get("reason", "")
        state.promoted_at = None
        state.demoted_at = None
        state.history = []
        state.last_audit_at = now
        return state, {
            "ts": now,
            "decision": "pick",
            "symbol": state.symbol,
            "previous_symbol": None,
            "history_len": 0,
            "promotion_check": {"eligible": False, "reason": "fresh_pick"},
            "alpha_decay": {"triggered": False, "reason": "fresh_pick"},
            "audit": {
                "symbol": state.symbol,
                "expected_daily_usd": pick.get("expected_daily_usd", 0.0),
                "pinned": pick.get("pinned", False),
                "control_symbol": state.control_symbol,
                "lot_multiplier": state.lot_multiplier,
            },
        }

    # -------------------- Switch on next-day pick -----------------------
    if pick["symbol"] != state.symbol:
        # 2026-07-22: stickiness — a STATUS_PROMOTED canary may only be
        # abandoned when (a) the new pick is at least 1.25x the prior
        # expected_daily_usd AND (b) the canary has been promoted for at
        # LEAST promotion_window_days (real day-delta, not just any later
        # timestamp). This guards against trinket projection-noise swaps
        # that would toss a winner to chase a marginal new stub.
        candidate_promoted = state.status == STATUS_PROMOTED
        promoted_long_enough = (
            _days_since_iso(state.promoted_at) >= promotion_window_days
        )
        expected_today = float(pick.get("expected_daily_usd", 0.0) or 0.0)
        prev_expected = (state.history[-1].get("expected_daily_usd", 0.0)
                         if state.history else 0.0)
        large_uplift = (
            prev_expected > 0
            and expected_today >= prev_expected * 1.25
        )
        if candidate_promoted and promoted_long_enough and not large_uplift:
            state.last_audit_at = now
            return state, {
                "ts": now,
                "decision": "sticky_promoted",
                "symbol": state.symbol,
                "candidate_symbol": pick["symbol"],
                "history_len": len(state.history),
                "promotion_check": {"eligible": False, "reason": "sticky"},
                "alpha_decay": {"triggered": False, "reason": "sticky"},
                "audit": {
                    "candidate": pick["symbol"],
                    "expected_daily_usd": pick["expected_daily_usd"],
                    "current_canary": state.symbol,
                    "reason": "promoted_canary_protected_from_trinket_swap",
                    "days_since_promoted": _days_since_iso(state.promoted_at),
                    "window_required_days": promotion_window_days,
                },
            }
        previous_symbol = state.symbol  # capture before mutation
        state.symbol = pick["symbol"]
        state.picked_at = now
        state.picked_reason = pick.get("reason", "")
        state.status = STATUS_PICKED
        state.promoted_at = None
        state.demoted_at = None
        state.history = []
        state.last_audit_at = now
        return state, {
            "ts": now,
            "decision": "switch",
            "symbol": state.symbol,
            "previous_symbol": previous_symbol,
            "history_len": len(state.history),
            "promotion_check": {"eligible": False, "reason": "switch"},
            "alpha_decay": {"triggered": False, "reason": "switch_path"},
            "audit": {
                "symbol": state.symbol,
                "previous_symbol": previous_symbol,
                "expected_daily_usd": pick.get("expected_daily_usd", 0.0),
                "pinned": pick.get("pinned", False),
            },
        }

    # --------------------- Run promotion + decay checks on
    # --------------------- the EXISTING candidate. ---------------------
    state.control_symbol = control_symbol
    state.lot_multiplier = lot_mult

    promotion_eligible = (
        state.status in {STATUS_PICKED, STATUS_PROMOTED}
        and len(state.history) >= promotion_window_days
    )
    canary_window = _realized_daily_series(
        realized_trades, state.symbol, promotion_window_days
    )
    control_window = _realized_daily_series(
        realized_trades, state.control_symbol, promotion_window_days
    )

    promotion_passed = False
    promotion_reason = "skipped"
    if promotion_eligible:
        # "perform > control by > projected_daily * 0.5 over the window"
        win_len = min(promotion_window_days, len(canary_window), len(control_window))
        if win_len < promotion_window_days:
            promotion_reason = f"insufficient_window({win_len}<{promotion_window_days})"
        else:
            canary_total = sum(canary_window[:win_len])
            control_total = sum(control_window[:win_len])
            diff = canary_total - control_total
            threshold = pick["expected_daily_usd"] * promoted_threshold * promotion_window_days
            if diff > threshold:
                promotion_passed = True
                promotion_reason = f"canary-control=${diff:.2f} > ${threshold:.2f}"
            else:
                promotion_reason = f"canary-control=${diff:.2f} ≤ ${threshold:.2f}"

    # Alpha decay: run on full decay_window_days window
    decay_window = _realized_daily_series(
        realized_trades, state.symbol, decay_window_days
    )
    decayed, decay_reason = _check_alpha_decay(
        decay_window, pick["expected_daily_usd"],
        sigma_threshold=sigma_threshold,
    )

    if decayed:
        state.status = STATUS_DECAYED
        state.demoted_at = now
        decision = "decay_demote"
    elif promotion_eligible and promotion_passed:
        state.status = STATUS_PROMOTED
        state.promoted_at = now
        decision = "promote"
    elif promotion_eligible and not promotion_passed:
        # 2026-07-22 (revised) — demote only when the comparison is
        # actually grounded in realized data. A quiet-market window
        # (all entries zero, no closed trades) MUST NOT trigger a demote,
        # otherwise no-realised-data sessions would falsely demote every
        # promoted canary. We require non-zero entries in EITHER the
        # canary realised window, the control realised window, or the
        # state.history canary_pnl_day column to demote; otherwise we
        # emit "continue_no_data" and stay PICKED.
        _decision_grounded = (
            _window_has_real_data(canary_window)
            or _window_has_real_data(control_window)
            or any(
                abs((h.get("canary_pnl_day") or 0.0)) > 1e-9
                or abs((h.get("control_pnl_day") or 0.0)) > 1e-9
                for h in state.history
            )
        )
        if _decision_grounded:
            state.status = STATUS_DEMOTED
            state.demoted_at = now
            decision = "demote"
        else:
            decision = "continue_no_data"
            promotion_reason = (
                f"{promotion_reason} (data-missing hold; not demoted)"
            )
    else:
        decision = "continue"

    # Idempotent history upsert: re-running on the same UTC day overwrites
    # the prior row, so `state.history` length never exceeds the number of
    # distinct days the canary has been observed (no inflation from
    # multiple cronjob runs).
    _upsert_today_history(state.history, {
        "ts": now,
        "decision": decision,
        "canary_pnl_day": canary_window[-1] if canary_window else 0.0,
        "control_pnl_day": control_window[-1] if control_window else 0.0,
        "expectancy_r": pick.get("expectancy_r", 0.0),
        "expected_daily_usd": pick.get("expected_daily_usd", 0.0),
    })
    # Bound history to last (max 90 days)
    state.history = state.history[-90:]
    state.last_audit_at = now

    audit = {
        "ts": now,
        "decision": decision,
        "symbol": state.symbol,
        "control_symbol": state.control_symbol,
        "lot_multiplier": state.lot_multiplier,
        "expectancy_r": pick.get("expectancy_r", 0.0),
        "expected_daily_usd": pick.get("expected_daily_usd", 0.0),
        "history_len": len(state.history),
        "promotion_check": {
            "eligible": promotion_eligible,
            "passed": promotion_passed if promotion_eligible else None,
            "reason": promotion_reason,
        },
        "alpha_decay": {
            "triggered": decayed,
            "reason": decay_reason,
            "window_days": len(decay_window),
        },
        "audit": {
            "symbol": state.symbol,
            "control_symbol": state.control_symbol,
            "lot_multiplier": state.lot_multiplier,
            "expectancy_r": pick.get("expectancy_r", 0.0),
            "expected_daily_usd": pick.get("expected_daily_usd", 0.0),
            "history_len": len(state.history),
        },
    }
    return state, audit


# ----------------------------------------------------------------------
# JSONL append helpers (do not touch config files)
# ----------------------------------------------------------------------
def state_path() -> Path:
    return STATE_DIR / CANARY_STATE_FILE


def audit_path() -> Path:
    return STATE_DIR / CANARY_AUDIT_FILE


def load_state(path: Path | None = None) -> CanaryState:
    p = path or state_path()
    if not p.exists():
        return CanaryState()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CanaryState()
    return CanaryState.from_dict(raw)


def save_state(state: CanaryState, path: Path | None = None) -> None:
    p = path or state_path()
    write_json_state(p.name, state.to_dict())


def append_audit(audit_row: dict[str, Any], path: Path | None = None) -> None:
    p = path or audit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(audit_row, default=str, ensure_ascii=False) + "\n")


# ----------------------------------------------------------------------
# Convenience: bootstrap CLI
# ----------------------------------------------------------------------
def latest_projection_files(state_dir: Path | None = None) -> list[Path]:
    sd = state_dir or STATE_DIR
    if not sd.exists():
        return []
    return sorted(sd.glob("edge_projection_*.json"), reverse=True)


def load_latest_projection(state_dir: Path | None = None) -> tuple[Path | None, list[dict[str, Any]]]:
    files = latest_projection_files(state_dir)
    if not files:
        return None, []
    try:
        doc = json.loads(files[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return files[0], []
    rows = doc.get("per_symbol", []) if isinstance(doc, dict) else []
    return files[0], rows


def lot_multiplier_for(
    symbol: str,
    state_path_arg: Path | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> float:
    """Return the lot multiplier to apply for ``symbol`` at entry time.

    Returns 1.0 when:
        - state file is missing or unparseable
        - canary is disabled / no_trusted_status
        - ``symbol`` is not the current canary pick

    The ``config`` arg is consulted ONLY when the canary feature is
    explicitly disabled there (e.g. ``canary.pinned_symbol == ""``);
    otherwise the persistent state is the source of truth (an operator
    who wants to override the current pick should delete the state file
    or run ``scripts/run_canary_pick.py`` with a pin).
    """
    if config is not None:
        canary_cfg = (config.get("canary") or {})
        if canary_cfg.get("pinned_symbol", "<UNSET>") == "":
            return 1.0
    p = state_path_arg or state_path()
    if not p.exists():
        return 1.0
    try:
        st = CanaryState.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return 1.0
    if st.status in {STATUS_INIT, STATUS_DISABLED, STATUS_NO_TRUSTED, STATUS_DECAYED, STATUS_DEMOTED}:
        return 1.0
    if st.symbol != symbol:
        return 1.0
    if st.lot_multiplier <= 1.0:
        return 1.0
    return float(st.lot_multiplier)
