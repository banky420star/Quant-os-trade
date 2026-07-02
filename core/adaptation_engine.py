"""Adaptation engine — detect and log behavioral changes from live trade outcomes."""

from __future__ import annotations

from typing import Any

from core.utils import utc_now_iso


def _veto_map(policy: dict[str, Any]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    symbols = (policy or {}).get("symbols") or {}
    if not isinstance(symbols, dict):
        return out
    for sym, row in symbols.items():
        cells = (row or {}).get("vetoed_cells") or []
        out[str(sym)] = set(str(c) for c in cells)
    return out


def diff_vetoes(
    old_policy: dict[str, Any],
    new_policy: dict[str, Any],
    ledger_cells: dict[str, dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Cells newly vetoed or cleared per symbol."""
    old_map = _veto_map(old_policy)
    new_map = _veto_map(new_policy)
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    all_syms = sorted(set(old_map) | set(new_map))
    cells_by_sym = ledger_cells or {}

    for sym in all_syms:
        old_cells = old_map.get(sym, set())
        new_cells = new_map.get(sym, set())
        for cell in sorted(new_cells - old_cells):
            st = (cells_by_sym.get(sym) or {}).get(cell) or {}
            added.append({
                "symbol": sym,
                "cell": cell,
                "n": st.get("n"),
                "win_rate_pct": st.get("win_rate_pct"),
                "expectancy_net_r": st.get("expectancy_net_r"),
                "verdict": st.get("verdict", "vetoed"),
            })
        for cell in sorted(old_cells - new_cells):
            st = (cells_by_sym.get(sym) or {}).get(cell) or {}
            removed.append({
                "symbol": sym,
                "cell": cell,
                "n": st.get("n"),
                "win_rate_pct": st.get("win_rate_pct"),
                "expectancy_net_r": st.get("expectancy_net_r"),
            })
    return {"added": added, "removed": removed}


def diff_be_trail(
    old: dict[str, Any],
    new: dict[str, Any],
) -> list[dict[str, Any]]:
    """Per-symbol BE/trailing calibration changes."""
    changes: list[dict[str, Any]] = []
    old_syms = (old or {}).get("symbols") or {}
    new_syms = (new or {}).get("symbols") or {}
    for sym in sorted(set(old_syms) | set(new_syms)):
        o = old_syms.get(sym) or {}
        n = new_syms.get(sym) or {}
        if not n:
            continue
        o_trusted = bool(o.get("trusted"))
        n_trusted = bool(n.get("trusted"))
        if o_trusted != n_trusted or (
            n_trusted and (
                o.get("expectancy_r") != n.get("expectancy_r")
                or o.get("break_even") != n.get("break_even")
                or o.get("trailing") != n.get("trailing")
            )
        ):
            changes.append({
                "symbol": sym,
                "trusted": n_trusted,
                "was_trusted": o_trusted,
                "expectancy_r": n.get("expectancy_r"),
                "seed_expectancy_r": n.get("seed_expectancy_r"),
                "win_rate_pct": n.get("win_rate_pct"),
                "n": n.get("n"),
                "break_even": n.get("break_even"),
                "trailing": n.get("trailing"),
                "reason": n.get("reason"),
            })
    return changes


def diff_edge_stats(
    old_stats: dict[str, Any],
    new_stats: dict[str, Any],
    *,
    min_delta_wr: float = 5.0,
) -> list[dict[str, Any]]:
    """Notable per-symbol setup win-rate shifts after new closes."""
    shifts: list[dict[str, Any]] = []
    old_by = (old_stats or {}).get("by_symbol") or {}
    new_by = (new_stats or {}).get("by_symbol") or {}
    for sym in sorted(set(old_by) | set(new_by)):
        o_setups = old_by.get(sym) or {}
        n_setups = new_by.get(sym) or {}
        for setup in sorted(set(o_setups) | set(n_setups)):
            o = o_setups.get(setup) or {}
            n = n_setups.get(setup) or {}
            o_n = int(o.get("total") or 0)
            n_n = int(n.get("total") or 0)
            if n_n <= o_n:
                continue
            o_wr = float(o.get("win_rate_pct") or 0)
            n_wr = float(n.get("win_rate_pct") or 0)
            if abs(n_wr - o_wr) >= min_delta_wr or n_n - o_n >= 3:
                shifts.append({
                    "symbol": sym,
                    "setup": setup,
                    "old_win_rate_pct": o_wr,
                    "new_win_rate_pct": n_wr,
                    "old_n": o_n,
                    "new_n": n_n,
                    "delta_n": n_n - o_n,
                })
    return shifts


def compile_adaptation_report(
    *,
    new_trade_count: int,
    new_records: list[dict[str, Any]],
    veto_diff: dict[str, list[dict[str, Any]]],
    be_trail_changes: list[dict[str, Any]],
    edge_shifts: list[dict[str, Any]],
) -> dict[str, Any]:
    """One adaptation cycle summary — what changed and why."""
    veto_added = veto_diff.get("added") or []
    veto_removed = veto_diff.get("removed") or []
    trusted = [c for c in be_trail_changes if c.get("trusted")]
    actions: list[str] = []
    if veto_added:
        actions.append(f"blocked {len(veto_added)} losing cell(s)")
    if veto_removed:
        actions.append(f"cleared {len(veto_removed)} veto(s)")
    if trusted:
        actions.append(f"applied BE/trail on {len(trusted)} symbol(s)")
    if edge_shifts:
        actions.append(f"updated rankings from {len(edge_shifts)} setup shift(s)")

    return {
        "timestamp": utc_now_iso(),
        "new_closes": new_trade_count,
        "closed_trades": [
            {
                "trade_id": r.get("trade_id"),
                "symbol": r.get("symbol"),
                "setup_type": r.get("setup_type"),
                "result": r.get("result"),
                "pnl": r.get("pnl"),
                "session": r.get("session"),
                "regime": r.get("regime"),
            }
            for r in new_records
        ],
        "actions": actions,
        "veto_added": veto_added,
        "veto_removed": veto_removed,
        "be_trail_changes": be_trail_changes,
        "edge_shifts": edge_shifts,
        "summary": (
            "; ".join(actions) if actions
            else "recorded closes — no policy change yet (thin data)"
        ),
    }