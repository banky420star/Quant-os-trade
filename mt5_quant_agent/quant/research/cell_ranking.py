"""Per-symbol setup×session cell ranking with culturing veto awareness."""

from __future__ import annotations

from typing import Any


def _culturing_cfg(config: dict[str, Any]) -> dict[str, Any]:
    cfg = config.get("culturing") or {}
    return {
        "min_n": int(cfg.get("min_n", 8)),
        "veto_win_rate_pct": float(cfg.get("veto_win_rate_pct", 40)),
        "promote_min_n": int(cfg.get("promote_min_n", 4)),
        "promote_min_win_rate_pct": float(cfg.get("promote_min_win_rate_pct", 48)),
        "promote_min_expectancy_r": float(cfg.get("promote_min_expectancy_r", 0.03)),
    }


def _parse_cell(cell: str) -> dict[str, str]:
    parts = str(cell or "").split("|")
    while len(parts) < 4:
        parts.append("?")
    return {
        "setup": parts[0],
        "regime": parts[1],
        "align": parts[2],
        "session": parts[3],
    }


def cell_matches_seed(cell: str, seed: dict[str, Any]) -> bool:
    """True when a culturing cell matches a seed edge (setup + session; regime wildcard)."""
    parsed = _parse_cell(cell)
    setup = str(seed.get("setup") or "")
    sessions = [str(s) for s in (seed.get("sessions") or [])]
    if setup and parsed["setup"] != setup:
        return False
    if sessions and parsed["session"] not in sessions:
        return False
    return bool(setup or sessions)


def seed_cells_from_config(config: dict[str, Any]) -> dict[str, list[str]]:
    """Build promoted cell keys from adaptation.seed_edge_cells (align wildcard)."""
    adapt = config.get("adaptation") or {}
    seeds = adapt.get("seed_edge_cells") or []
    out: dict[str, list[str]] = {}
    if not isinstance(seeds, list):
        return out
    for row in seeds:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "")
        setup = str(row.get("setup") or "unknown")
        for session in row.get("sessions") or []:
            key = f"{setup}|*|align|{session}"
            out.setdefault(sym, []).append(key)
    return out


def rank_cells_for_symbol(
    symbol: str,
    cells: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    vetoed: set[str] | None = None,
    promoted: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Rank culturing cells by realized net expectancy (vetoed cells sink)."""
    cconf = _culturing_cfg(config or {})
    quant_sym = ((config or {}).get("quant") or {}).get("per_symbol", {}).get(symbol, {}) or {}
    preferred_setups = set(quant_sym.get("preferred_setups") or [])
    preferred_sessions = set(quant_sym.get("preferred_sessions") or [])
    seed_map = seed_cells_from_config(config or {})
    seed_rows = seed_map.get(symbol, [])

    ranked: list[dict[str, Any]] = []
    vetoed = vetoed or set()
    promoted = promoted or set()

    for cell, stats in (cells or {}).items():
        if not isinstance(stats, dict):
            continue
        parsed = _parse_cell(cell)
        n = int(stats.get("n") or 0)
        exp = float(stats.get("expectancy_net_r") or stats.get("expectancy_r") or 0)
        wr = float(stats.get("win_rate_pct") or 0)
        verdict = str(stats.get("verdict") or "thin")
        is_vetoed = cell in vetoed or verdict == "vetoed"
        is_promoted = cell in promoted or any(cell_matches_seed(cell, s) for s in (config or {}).get("adaptation", {}).get("seed_edge_cells", []) if s.get("symbol") == symbol)
        score = exp * 100.0 + wr * 0.25
        if is_promoted:
            score += 25.0
        if parsed["setup"] in preferred_setups:
            score += 8.0
        if parsed["session"] in preferred_sessions:
            score += 6.0
        if is_vetoed:
            score -= 200.0
        if n < cconf["min_n"]:
            score -= 5.0
        ranked.append({
            "symbol": symbol,
            "cell": cell,
            "setup": parsed["setup"],
            "session": parsed["session"],
            "n": n,
            "win_rate_pct": wr,
            "expectancy_net_r": exp,
            "verdict": "vetoed" if is_vetoed else verdict,
            "promoted": is_promoted,
            "seed_match": any(cell_matches_seed(cell, {"setup": p.split("|")[0], "sessions": [p.split("|")[3]]}) for p in seed_rows) if seed_rows else False,
            "score": round(score, 3),
        })

    ranked.sort(key=lambda r: (-float(r["score"]), -float(r["expectancy_net_r"]), -int(r["n"])))
    for i, row in enumerate(ranked):
        row["rank"] = i + 1
    return ranked


def demote_vetoed_setups(
    rankings: list[dict[str, Any]],
    *,
    cells: dict[str, Any],
    vetoed: set[str],
) -> list[dict[str, Any]]:
    """Penalize setups whose culturing cells are predominantly vetoed."""
    if not rankings or not vetoed:
        return rankings

    setup_veto_frac: dict[str, float] = {}
    setup_counts: dict[str, int] = {}
    for cell, stats in (cells or {}).items():
        if not isinstance(stats, dict):
            continue
        parsed = _parse_cell(cell)
        setup = parsed["setup"]
        setup_counts[setup] = setup_counts.get(setup, 0) + 1
        if cell in vetoed or stats.get("verdict") == "vetoed":
            setup_veto_frac[setup] = setup_veto_frac.get(setup, 0) + 1

    out: list[dict[str, Any]] = []
    for row in rankings:
        setup = row.get("setup_type") or row.get("setup")
        if not setup:
            out.append(row)
            continue
        veto_n = setup_veto_frac.get(setup, 0)
        total = setup_counts.get(setup, 0)
        frac = veto_n / max(total, 1)
        patched = dict(row)
        if frac >= 0.5 and veto_n >= 2:
            patched["score"] = float(patched.get("score", 0)) - 30.0
            patched["cell_veto_demoted"] = True
        out.append(patched)

    out.sort(key=lambda r: (-float(r.get("score", 0)), r.get("rank", 999)))
    for i, row in enumerate(out):
        row["rank"] = i + 1
    return out


def promotion_candidates(
    symbol: str,
    cells: dict[str, Any],
    config: dict[str, Any],
    *,
    vetoed: set[str] | None = None,
) -> list[str]:
    """Cells that qualify for auto-promotion from positive live expectancy."""
    cconf = _culturing_cfg(config)
    vetoed = vetoed or set()
    promoted: list[str] = []
    for cell, stats in (cells or {}).items():
        if not isinstance(stats, dict):
            continue
        if cell in vetoed or stats.get("verdict") == "vetoed":
            continue
        n = int(stats.get("n") or 0)
        if n < cconf["promote_min_n"]:
            continue
        wr = float(stats.get("win_rate_pct") or 0)
        exp = float(stats.get("expectancy_net_r") or stats.get("expectancy_r") or 0)
        if exp >= cconf["promote_min_expectancy_r"] and wr >= cconf["promote_min_win_rate_pct"]:
            promoted.append(cell)
    return sorted(promoted)