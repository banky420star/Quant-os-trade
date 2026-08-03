"""Replay-evidence per-symbol veto (iteration 12, 2026-08-01).

Turns the historical-replay labeler's per-symbol evidence into an actionable
per-symbol blocklist: which (symbol, setup) cells are *reliable losers* and
should be blocked from firing on that symbol.

This is the "eventually becoming very symbol specific" lever — the system
doesn't just measure what works per symbol, it ACTS on it by blocking the
proven losers per symbol. Read by ``core.specialized_setups.detect_specialized``
when ``signals.specialized_setups.apply_replay_veto`` is true (default off,
operator opt-in; even on, it only filters which specialized setups fire and
places no orders).

HONESTY (matters — per VERDICT.md):

  * Only block RELIABLE losers. A cell is vetoed iff:
      n >= min_n  AND  expectancy_r < 0  AND  ci95_upper <= max_ci95_upper
    i.e. the bootstrap 95% CI upper bound is still <= 0 — the cell loses and the
    data is consistent with a true negative mean. This is much safer than
    blocking on point expectancy<0 alone (which would nuke half the cells by
    noise). A reliable loser is also far less likely to be a selection artifact
    than a reliable winner (there is no selection FOR losers), so the negative
    side of the CI is the trustworthy direction here.

  * Carries the DSR/SPA caveat. The replay report already says per-cell CI95 is
    selection-biased across ~80-100 cells. This veto is RISK HYGIENE derived
    from one ~40-day window, not a deploy signal. Re-derive periodically (each
    replay run) so the veto tracks regime drift; do not freeze it.

  * Never auto-applies to live trading. It writes state/specialized_replay_veto.json
    for operator review; ``apply_replay_veto`` must be explicitly enabled in
    config for detect_specialized to consume it. ``enabled`` (the order path)
    stays false per the standing no-orders constraint.

This module places no orders, touches no kill switch, does no live trading.
"""

from __future__ import annotations

from typing import Any

from core.utils import read_json_state, utc_now_iso, write_json_state

REPORT_NAME = "specialized_replay_report.json"
VETO_NAME = "specialized_replay_veto.json"


def build_replay_veto(
    report: dict[str, Any],
    *,
    min_n: int = 8,
    max_ci95_upper: float = 0.0,
) -> dict[str, Any]:
    """From a replay report, emit the per-symbol blocklist of reliable losers.

    ``report`` = output of core.specialized_replay_labeler.aggregate_cells / run
    (must contain ``cells`` with n, expectancy_r, ci95_lower, ci95_upper).

    Returns:
      {timestamp, per_symbol: {symbol: [setup, ...]}, cells_vetoed: [...],
       min_n, max_ci95_upper, caveat}
    """
    cells = report.get("cells") or []
    per_symbol: dict[str, list[str]] = {}
    vetoed: list[dict[str, Any]] = []
    for c in cells:
        try:
            n = int(c.get("n") or 0)
            exp = float(c.get("expectancy_r") or 0.0)
            ci_hi = c.get("ci95_upper")
        except (TypeError, ValueError):
            continue
        if n < min_n:
            continue
        if exp >= 0:
            continue
        # ci95_upper may be None for very thin cells (n<2) — those are already
        # filtered by min_n>=8, but guard: treat None as "no upper bound" -> skip.
        if ci_hi is None:
            continue
        try:
            ci_hi = float(ci_hi)
        except (TypeError, ValueError):
            continue
        if ci_hi > max_ci95_upper:
            continue  # CI still includes positive territory -> not reliable enough
        sym = str(c.get("symbol") or "")
        setup = str(c.get("setup_type") or "")
        if not sym or not setup:
            continue
        per_symbol.setdefault(sym, []).append(setup)
        vetoed.append({
            "symbol": sym, "setup_type": setup, "n": n,
            "expectancy_r": round(exp, 4), "ci95_upper": round(ci_hi, 4),
        })

    # de-dup + sort for stable output
    per_symbol = {sym: sorted(set(setups)) for sym, setups in per_symbol.items()}

    return {
        "timestamp": utc_now_iso(),
        "per_symbol": per_symbol,
        "cells_vetoed": vetoed,
        "min_n": min_n,
        "max_ci95_upper": max_ci95_upper,
        "vetoed_cell_count": len(vetoed),
        "symbols_affected": len(per_symbol),
        "caveat": (
            "Replay-evidence veto = RISK HYGIENE from one ~40-day window, NOT a "
            "deploy signal. A cell is vetoed only if n>=min_n AND expectancy<0 "
            "AND the bootstrap CI95 upper bound <= 0 (reliably negative). Re-derive "
            "each replay run; do not freeze. Per VERDICT.md per-cell CI95 is "
            "selection-biased across ~100 cells — the negative side is the "
            "trustworthy direction (no selection FOR losers)."
        ),
    }


def run(*, persist: bool = True, min_n: int = 8, max_ci95_upper: float = 0.0) -> dict[str, Any]:
    """Read the latest replay report and write the per-symbol veto."""
    report = read_json_state(REPORT_NAME, default={}) or {}
    if not report.get("cells"):
        empty = {
            "timestamp": utc_now_iso(), "per_symbol": {}, "cells_vetoed": [],
            "vetoed_cell_count": 0, "symbols_affected": 0,
            "note": f"no replay report found at state/{REPORT_NAME} — run specialized_replay_labeler.run first",
        }
        if persist:
            write_json_state(VETO_NAME, empty)
        return empty
    veto = build_replay_veto(report, min_n=min_n, max_ci95_upper=max_ci95_upper)
    if persist:
        write_json_state(VETO_NAME, veto)
    return veto