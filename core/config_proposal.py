"""Bounded config-proposal engine (Phase 2.4).

Aggregates trade reviews into small, safe config-change proposals. Every
proposal is bounded (tiny deltas) and passed through a safety policy that
rejects anything touching lot size, credentials, account mode, or the
protective guards (emergency exit / spread guard / daily-loss / max-drawdown).
Nothing is applied here — the loop decides that by mode.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from core.utils import utc_now_iso

# --- Safety policy ---------------------------------------------------------

# Config paths the engine may never touch automatically.
FORBIDDEN_PATHS = {
    ("execution", "default_lot"),
    ("execution", "max_lot"),
    ("execution", "live_trading_enabled"),
    ("execution", "mt5_trading_enabled"),
    ("execution", "allow_live_account"),
    ("execution", "starting_cash"),
    ("execution", "magic_number"),
    ("mt5", "account_mode"),
    ("mt5", "login"),
    ("mt5", "password"),
    ("mt5", "server"),
    ("mt5", "path"),
    ("risk", "max_drawdown_pct"),
    ("risk", "kill_switch"),
    ("risk", "max_consecutive_losses"),
    ("practice", "micro", "max_lot"),
    ("practice", "micro", "default_lot"),
}

# Guards that must never be disabled by a proposal.
GUARD_DISABLE_PATHS = {
    ("fast_mode", "emergency_exit", "enabled"),
    ("trading", "break_even", "enabled"),
    ("filters", "avoid_news"),
}

# Bounded delta ranges per tunable (path -> (min_delta, max_delta, step))
BOUNDED_TUNABLES: dict[tuple[str, ...], tuple[float, float]] = {
    ("trading", "sl_tp", "tp1_rr"): (-0.2, 0.2),
    ("trading", "sl_tp", "tp2_rr"): (-0.3, 0.3),
    ("trading", "sl_tp", "sl_atr_mult"): (-0.2, 0.2),
    ("signals", "min_confidence"): (-10.0, 10.0),
    ("filters", "spread_mult"): (-0.5, 0.2),
    ("fast_mode", "max_trades_per_symbol_per_hour"): (-4.0, 2.0),
    ("trading", "strategy_entries", "entry_buffer_atr"): (-0.05, 0.05),
    ("trading", "trailing", "activation_atr_mult"): (-0.2, 0.2),
    ("trading", "break_even", "trigger_atr_mult"): (-0.2, 0.2),
}

MIN_TRADES_FOR_CAP_RELAX = 30


def _clamp_delta(path: tuple[str, ...], delta: float) -> float:
    lo, hi = BOUNDED_TUNABLES.get(path, (0.0, 0.0))
    return round(max(lo, min(hi, float(delta))), 4)


def is_dangerous(patch: dict[str, Any], *, reviewed_trades: int = 0) -> tuple[bool, str]:
    """Return (dangerous, reason). A dangerous patch must be rejected."""
    for op in patch.get("ops") or []:
        p = tuple(op.get("path") or [])
        if p in FORBIDDEN_PATHS:
            return True, f"forbidden_path:{'.'.join(p)}"
        if p in GUARD_DISABLE_PATHS and op.get("value") is False:
            return True, f"guard_disabled:{'.'.join(p)}"
        # Relaxing a protective limit downward is dangerous.
        if p == ("filters", "spread_mult") and float(op.get("value") or 0) > float(op.get("prev") or 0):
            return True, "spread_guard_loosened"
        if p == ("fast_mode", "max_trades_per_symbol_per_hour") and float(op.get("value") or 0) > float(op.get("prev") or 0):
            if reviewed_trades < MIN_TRADES_FOR_CAP_RELAX:
                return True, f"cap_relaxed_without_evidence({reviewed_trades}<{MIN_TRADES_FOR_CAP_RELAX})"
        # No lot-size / mode increases ever
        if p in {("execution", "default_lot"), ("execution", "max_lot"), ("practice", "micro", "max_lot")}:
            if float(op.get("value") or 0) > float(op.get("prev") or 0):
                return True, "lot_size_increased"
    return False, ""


def _make_op(path: tuple[str, ...], prev: float, delta: float) -> dict[str, Any]:
    new_val = round(float(prev) + delta, 4)
    return {"path": list(path), "prev": prev, "delta": delta, "value": new_val}


def _proposal_id(symbol: str, kind: str) -> str:
    return f"{symbol.lower()}_{kind}_{utc_now_iso()[:19].replace(':','').replace('-','')}"


def _build_proposal(
    *, symbol: str, kind: str, reason: str, evidence: dict[str, Any],
    ops: list[dict[str, Any]], risk_level: str, mode_required: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    patch = {"ops": ops}
    dangerous, why = is_dangerous(patch, reviewed_trades=int(evidence.get("reviewed_trades", 0)))
    auto_apply = (not dangerous) and risk_level == "low"
    return {
        "proposal_id": _proposal_id(symbol, kind),
        "created_at": utc_now_iso(),
        "symbol": symbol,
        "kind": kind,
        "reason": reason,
        "evidence": evidence,
        "patch": patch,
        "risk_level": risk_level,
        "mode_required": mode_required,
        "auto_apply_allowed": auto_apply,
        "rejected": dangerous,
        "rejection_reason": why if dangerous else None,
        "config_hash_before": None,
    }


def _get_path(config: dict[str, Any], path: tuple[str, ...]) -> float:
    cur: Any = config
    for k in path:
        if not isinstance(cur, dict):
            return 0.0
        cur = cur.get(k)
    try:
        return float(cur)
    except (TypeError, ValueError):
        return 0.0


def propose_from_reviews(
    reviews: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    min_sample: int = 3,
) -> list[dict[str, Any]]:
    """Aggregate reviews into bounded proposals (one per symbol+kind pattern)."""
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in reviews:
        if r.get("symbol"):
            by_sym[r["symbol"]].append(r)

    proposals: list[dict[str, Any]] = []
    for symbol, rows in by_sym.items():
        n = len(rows)
        reviewed = n
        # TP too early pattern -> widen TP
        tp_early = [r for r in rows if "tp_too_early" in (r.get("mistake_categories") or [])]
        if len(tp_early) >= min_sample:
            def _missed(r):
                post = float(r.get("post_exit_max_favorable_atr") or 0)
                mfe = r.get("mfe_R"); rm = r.get("r_multiple")
                via_mfe = (float(mfe) - float(rm)) if (mfe is not None and rm is not None) else 0.0
                return max(post, via_mfe)
            avg_missed = sum(_missed(r) for r in tp_early) / len(tp_early)
            if avg_missed >= 0.3:
                prev = _get_path(config, ("trading", "sl_tp", "tp1_rr")) or 1.5
                delta = _clamp_delta(("trading", "sl_tp", "tp1_rr"), min(0.15, 0.05 * round(avg_missed / 0.3)))
                proposals.append(_build_proposal(
                    symbol=symbol, kind="tp_too_early",
                    reason=f"{len(tp_early)} winners exited early; avg missed move {avg_missed:.2f} ATR.",
                    evidence={"sample_size": len(tp_early), "avg_missed_move_atr": round(avg_missed, 3),
                              "current_tp1_rr": prev, "reviewed_trades": reviewed},
                    ops=[_make_op(("trading", "sl_tp", "tp1_rr"), prev, delta)],
                    risk_level="low", mode_required="shadow_apply", config=config,
                ))

        # SL too tight pattern -> widen SL
        sl_tight = [r for r in rows if "sl_too_tight" in (r.get("mistake_categories") or [])]
        if len(sl_tight) >= min_sample:
            prev = _get_path(config, ("trading", "sl_tp", "sl_atr_mult")) or 0.5
            delta = _clamp_delta(("trading", "sl_tp", "sl_atr_mult"), 0.1)
            proposals.append(_build_proposal(
                symbol=symbol, kind="sl_too_tight",
                reason=f"{len(sl_tight)} losses stopped on small wiggles then reversed.",
                evidence={"sample_size": len(sl_tight), "current_sl_atr_mult": prev, "reviewed_trades": reviewed},
                ops=[_make_op(("trading", "sl_tp", "sl_atr_mult"), prev, delta)],
                risk_level="low", mode_required="shadow_apply", config=config,
            ))

        # Wrong timeframe alignment -> raise min confidence (require better entries)
        wtf = [r for r in rows if "wrong_timeframe_alignment" in (r.get("mistake_categories") or [])]
        if len(wtf) >= min_sample:
            prev = _get_path(config, ("signals", "min_confidence")) or 50
            delta = _clamp_delta(("signals", "min_confidence"), 5)
            proposals.append(_build_proposal(
                symbol=symbol, kind="wrong_tf_alignment",
                reason=f"{len(wtf)} trades entered against the higher timeframe.",
                evidence={"sample_size": len(wtf), "current_min_confidence": prev, "reviewed_trades": reviewed},
                ops=[_make_op(("signals", "min_confidence"), prev, delta)],
                risk_level="low", mode_required="shadow_apply", config=config,
            ))

        # Overtrading -> lower the per-hour cap
        ot = [r for r in rows if "overtrading" in (r.get("mistake_categories") or [])]
        if len(ot) >= min_sample:
            prev = _get_path(config, ("fast_mode", "max_trades_per_symbol_per_hour")) or 4
            delta = _clamp_delta(("fast_mode", "max_trades_per_symbol_per_hour"), -1)
            proposals.append(_build_proposal(
                symbol=symbol, kind="overtrading",
                reason=f"{len(ot)} reviews flagged overtrading on the symbol.",
                evidence={"sample_size": len(ot), "current_cap": prev, "reviewed_trades": reviewed},
                ops=[_make_op(("fast_mode", "max_trades_per_symbol_per_hour"), prev, delta)],
                risk_level="low", mode_required="shadow_apply", config=config,
            ))

        # Spread spike -> tighten spread guard
        sp = [r for r in rows if "spread_spike_entry" in (r.get("mistake_categories") or [])]
        if len(sp) >= min_sample:
            prev = _get_path(config, ("filters", "spread_mult")) or 3.5
            delta = _clamp_delta(("filters", "spread_mult"), -0.2)
            proposals.append(_build_proposal(
                symbol=symbol, kind="spread_spike",
                reason=f"{len(sp)} entries taken on elevated spreads.",
                evidence={"sample_size": len(sp), "current_spread_mult": prev, "reviewed_trades": reviewed},
                ops=[_make_op(("filters", "spread_mult"), prev, delta)],
                risk_level="low", mode_required="shadow_apply", config=config,
            ))

    return proposals


def apply_patch_to_config(config: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of config with a patch's ops applied (used by shadow/live modes)."""
    import copy
    out = copy.deepcopy(config)
    for op in patch.get("ops") or []:
        path = list(op.get("path") or [])
        if not path:
            continue
        cur: Any = out
        for k in path[:-1]:
            cur = cur.setdefault(k, {}) if isinstance(cur, dict) else {}
        if isinstance(cur, dict):
            cur[path[-1]] = op.get("value")
    return out
