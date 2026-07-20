"""Auto-promote winning setup×session cells and preserve culturing vetoes."""

from __future__ import annotations

from typing import Any

from core.utils import utc_now_iso
from quant.research.cell_ranking import (
    promotion_candidates,
    rank_cells_for_symbol,
    seed_cells_from_config,
)


def _adapt_cfg(config: dict[str, Any]) -> dict[str, Any]:
    adapt = config.get("adaptation") or {}
    return {
        "auto_evolve_cells": bool(adapt.get("auto_evolve_cells", False)),
        "evolve_symbols": list(adapt.get("evolve_symbols") or []),
        "seed_edge_cells": list(adapt.get("seed_edge_cells") or []),
    }


def _expand_seed_cells(config: dict[str, Any], symbols: list[str]) -> dict[str, list[str]]:
    """Merge config seeds with any concrete culturing cells that match them."""
    seeds = seed_cells_from_config(config)
    for sym in symbols:
        for row in _adapt_cfg(config)["seed_edge_cells"]:
            if not isinstance(row, dict) or str(row.get("symbol")) != sym:
                continue
            setup = str(row.get("setup") or "unknown")
            for session in row.get("sessions") or []:
                seeds.setdefault(sym, []).append(f"{setup}|*|align|{session}")
    for sym in seeds:
        seeds[sym] = sorted(set(seeds[sym]))
    return seeds


def evolve_symbol_policy(
    ledger: dict[str, Any],
    config: dict[str, Any],
    *,
    existing_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Positive evolution: promote winners, keep forward-test vetoes for micro symbols."""
    acfg = _adapt_cfg(config)
    if not acfg["auto_evolve_cells"]:
        return existing_policy or {}

    symbols = acfg["evolve_symbols"] or list((config.get("mt5") or {}).get("symbols") or [])
    cells_by_sym = (ledger or {}).get("cells") or {}
    vetoed_by_sym = (ledger or {}).get("vetoed") or {}
    if not vetoed_by_sym and isinstance(ledger, dict):
        # forward_test_loop returns vetoed inside build_ledger but ledger_payload uses cells only;
        # reconstruct from cell verdicts when needed.
        for sym, sym_cells in cells_by_sym.items():
            vetoed_by_sym[sym] = [
                c for c, st in (sym_cells or {}).items()
                if isinstance(st, dict) and st.get("verdict") == "vetoed"
            ]

    seed_promoted = _expand_seed_cells(config, symbols)
    now = utc_now_iso()
    cconf = config.get("culturing") or {}

    policy_symbols: dict[str, dict[str, Any]] = {}
    evolution_rows: list[dict[str, Any]] = []

    for sym in symbols:
        sym_cells = cells_by_sym.get(sym) or {}
        vetoed = set(vetoed_by_sym.get(sym) or [])
        data_promoted = promotion_candidates(sym, sym_cells, config, vetoed=vetoed)
        promoted = sorted(set(seed_promoted.get(sym, [])) | set(data_promoted))
        rankings = rank_cells_for_symbol(
            sym,
            sym_cells,
            config=config,
            vetoed=vetoed,
            promoted=set(promoted),
        )
        policy_symbols[sym] = {
            "vetoed_cells": sorted(vetoed),
            "promoted_cells": promoted,
            "cell_rankings": rankings[:12],
        }
        evolution_rows.append({
            "symbol": sym,
            "promoted_added": len(data_promoted),
            "promoted_total": len(promoted),
            "vetoed_total": len(vetoed),
            "top_cell": rankings[0] if rankings else None,
        })

    payload = {
        "updated_at": now,
        "min_n": int(cconf.get("min_n", 8)),
        "veto_win_rate_pct": float(cconf.get("veto_win_rate_pct", 40)),
        "evolution": {
            "profile": config.get("active_profile"),
            "approach": "session_edge_specialist",
            "symbols": evolution_rows,
            "positive_evolution": any(r.get("promoted_added", 0) > 0 for r in evolution_rows),
        },
        "symbols": policy_symbols,
    }

    if existing_policy:
        # Preserve any symbols outside evolve set from the prior policy.
        old_syms = (existing_policy.get("symbols") or {}) if isinstance(existing_policy, dict) else {}
        for sym, row in old_syms.items():
            if sym not in policy_symbols:
                policy_symbols[sym] = row
        payload["symbols"] = policy_symbols

    return payload