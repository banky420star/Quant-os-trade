"""Positive evolution — trade only culturing cells with proven positive expectancy.

Candidate-1 \"Culturing Evolution\" approach:
  * Forward-test ledger cells with n >= min_n must show expectancy_net_r > 0
  * Edge-database high-edge patterns reinforce allowed cells (research_loop)
  * Thin cells stay permissive so the bot keeps collecting data
"""

from __future__ import annotations

import logging
from typing import Any

from core.edge_database import EdgeDatabase
from core.utils import read_json_state, utc_now_iso, write_json_state

STATE_FILE = "positive_evolution.json"


def positive_evolution_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Resolved positive-evolution gate settings."""
    micro = (config.get("practice") or {}).get("micro") or {}
    quant_pe = (config.get("quant") or {}).get("positive_evolution") or {}
    micro_pe = micro.get("positive_evolution") or {}
    pe = {**quant_pe, **micro_pe} if isinstance(micro_pe, dict) else dict(quant_pe)

    culturing = config.get("culturing") or {}
    enabled = bool(
        pe.get("enabled")
        or micro.get("positive_evolution_enabled")
        or (config.get("quant") or {}).get("positive_evolution_enabled")
    )
    return {
        "enabled": enabled,
        "min_n": int(pe.get("min_n", culturing.get("min_n", 8))),
        "min_expectancy_net_r": float(pe.get("min_expectancy_net_r", 0.0)),
        "edge_db_min_samples": int(pe.get("edge_db_min_samples", 5)),
        "edge_db_min_win_rate_pct": float(pe.get("edge_db_min_win_rate_pct", 55.0)),
        "edge_db_min_avg_pnl": float(pe.get("edge_db_min_avg_pnl", 0.0)),
        "symbols": list(micro.get("symbols") or (config.get("mt5") or {}).get("symbols") or []),
    }


def positive_evolution_active(config: dict[str, Any]) -> bool:
    return bool(positive_evolution_settings(config).get("enabled"))


def _edge_pattern_cells(pattern: dict[str, Any]) -> list[str]:
    """Map research_loop edge pattern to culturing cell keys (align + counter)."""
    setup = str(pattern.get("setup") or "?")
    regime = str(pattern.get("regime") or "?")
    session = str(pattern.get("session") or "?")
    return [
        f"{setup}|{regime}|align|{session}",
        f"{setup}|{regime}|counter|{session}",
    ]


def _cells_from_ledger(
    ledger_cells: dict[str, dict[str, Any]],
    cfg: dict[str, Any],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (allowed_cells, blocked_cells) per symbol from forward-test ledger."""
    allowed: dict[str, list[str]] = {}
    blocked: dict[str, list[str]] = {}
    min_n = int(cfg["min_n"])
    min_exp = float(cfg["min_expectancy_net_r"])

    for symbol, cell_map in (ledger_cells or {}).items():
        sym_allowed: list[str] = []
        sym_blocked: list[str] = []
        for cell, stats in (cell_map or {}).items():
            n = int((stats or {}).get("n") or 0)
            if n < min_n:
                continue
            exp = float((stats or {}).get("expectancy_net_r") or 0)
            if exp > min_exp:
                sym_allowed.append(cell)
            else:
                sym_blocked.append(cell)
        allowed[symbol] = sorted(sym_allowed)
        blocked[symbol] = sorted(sym_blocked)
    return allowed, blocked


def _cells_from_edge_db(config: dict[str, Any], cfg: dict[str, Any]) -> dict[str, list[str]]:
    """High-edge patterns from edge_database aggregates (research_loop shape)."""
    db = EdgeDatabase()
    aggregates = db.load().get("aggregates", {}) or {}
    by_context = aggregates.get("by_context", {}) or {}
    min_samples = int(cfg["edge_db_min_samples"])
    min_wr = float(cfg["edge_db_min_win_rate_pct"])
    min_pnl = float(cfg["edge_db_min_avg_pnl"])
    symbols = set(cfg.get("symbols") or [])

    out: dict[str, list[str]] = {}
    for ctx_key, setups in by_context.items():
        parts = str(ctx_key).split("|")
        if len(parts) < 3:
            continue
        symbol, regime, session = parts[0], parts[1], parts[2]
        if symbols and symbol not in symbols:
            continue
        for setup, stats in (setups or {}).items():
            total = int((stats or {}).get("total") or 0)
            if total < min_samples:
                continue
            wr = float((stats or {}).get("win_rate_pct") or 0)
            avg_pnl = float((stats or {}).get("avg_pnl") or 0)
            if wr < min_wr or avg_pnl <= min_pnl:
                continue
            pattern = {
                "setup": setup,
                "symbol": symbol,
                "regime": regime,
                "session": session,
            }
            out.setdefault(symbol, []).extend(_edge_pattern_cells(pattern))

    for sym in out:
        out[sym] = sorted(set(out[sym]))
    return out


def build_positive_evolution_state(
    config: dict[str, Any],
    *,
    ledger_cells: dict[str, dict[str, Any]] | None = None,
    edge_patterns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Merge ledger + edge-db evidence into verifier-readable state."""
    cfg = positive_evolution_settings(config)
    if ledger_cells is None:
        ledger_doc = read_json_state("forward_test_ledger.json", default={}) or {}
        ledger_cells = ledger_doc.get("cells") or {}

    ledger_allowed, ledger_blocked = _cells_from_ledger(ledger_cells, cfg)
    edge_allowed = _cells_from_edge_db(config, cfg)

    if edge_patterns:
        for pat in edge_patterns:
            if pat.get("type") != "high_edge":
                continue
            sym = str(pat.get("symbol") or "")
            if not sym:
                continue
            edge_allowed.setdefault(sym, []).extend(_edge_pattern_cells(pat))
        for sym in edge_allowed:
            edge_allowed[sym] = sorted(set(edge_allowed[sym]))

    symbols_payload: dict[str, dict[str, Any]] = {}
    all_symbols = sorted(
        set(cfg.get("symbols") or [])
        | set(ledger_allowed)
        | set(ledger_blocked)
        | set(edge_allowed)
    )
    for sym in all_symbols:
        ledger_pos = set(ledger_allowed.get(sym, []))
        edge_pos = set(edge_allowed.get(sym, []))
        merged_allowed = sorted(ledger_pos | edge_pos)
        symbols_payload[sym] = {
            "allowed_cells": merged_allowed,
            "ledger_positive": sorted(ledger_pos),
            "edge_db_positive": sorted(edge_pos),
            "blocked_cells": ledger_blocked.get(sym, []),
            "gate_mode": "positive_only" if merged_allowed or ledger_blocked.get(sym) else "collecting",
        }

    return {
        "updated_at": utc_now_iso(),
        "enabled": cfg["enabled"],
        "config": {
            "min_n": cfg["min_n"],
            "min_expectancy_net_r": cfg["min_expectancy_net_r"],
            "edge_db_min_samples": cfg["edge_db_min_samples"],
            "edge_db_min_win_rate_pct": cfg["edge_db_min_win_rate_pct"],
        },
        "symbols": symbols_payload,
        "total_allowed": sum(len(v.get("allowed_cells") or []) for v in symbols_payload.values()),
        "total_blocked": sum(len(v.get("blocked_cells") or []) for v in symbols_payload.values()),
    }


def refresh_positive_evolution(
    config: dict[str, Any],
    *,
    ledger_cells: dict[str, dict[str, Any]] | None = None,
    edge_patterns: list[dict[str, Any]] | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Rebuild and persist positive-evolution state."""
    log = logger or logging.getLogger("positive_evolution")
    cfg = positive_evolution_settings(config)
    if not cfg["enabled"]:
        return read_json_state(STATE_FILE, default={}) or {}

    if edge_patterns is None:
        try:
            from loops.research_loop import _find_patterns
            edge_patterns = _find_patterns(EdgeDatabase(log))
        except Exception as exc:  # noqa: BLE001
            log.warning("Edge pattern scan skipped: %s", exc)
            edge_patterns = []

    payload = build_positive_evolution_state(
        config,
        ledger_cells=ledger_cells,
        edge_patterns=edge_patterns,
    )
    write_json_state(STATE_FILE, payload)
    log.info(
        "Positive evolution: %d allowed cells, %d blocked across %d symbols",
        payload.get("total_allowed", 0),
        payload.get("total_blocked", 0),
        len(payload.get("symbols") or {}),
    )
    return payload


def evaluate_cell_gate(
    config: dict[str, Any],
    symbol: str,
    cell_key: str,
    state: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason_code) for a culturing cell."""
    cfg = positive_evolution_settings(config)
    if not cfg["enabled"]:
        return True, "disabled"

    sym_cfg = cfg.get("symbols") or []
    if sym_cfg and symbol not in sym_cfg:
        return True, "symbol_out_of_scope"

    state = state if state is not None else (read_json_state(STATE_FILE, default={}) or {})
    sym_state = (state.get("symbols") or {}).get(symbol) or {}
    blocked = set(sym_state.get("blocked_cells") or [])
    if cell_key in blocked:
        return False, "non_positive_expectancy"

    ledger_doc = read_json_state("forward_test_ledger.json", default={}) or {}
    cell_stats = ((ledger_doc.get("cells") or {}).get(symbol) or {}).get(cell_key) or {}
    n = int(cell_stats.get("n") or 0)
    min_n = int(cfg["min_n"])
    if n >= min_n:
        exp = float(cell_stats.get("expectancy_net_r") or 0)
        if exp <= float(cfg["min_expectancy_net_r"]):
            return False, "non_positive_expectancy"

    return True, "ok"


def diff_positive_cells(
    old_state: dict[str, Any],
    new_state: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Cells newly allowed or blocked between adaptation cycles."""
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    old_syms = (old_state or {}).get("symbols") or {}
    new_syms = (new_state or {}).get("symbols") or {}

    for sym in sorted(set(old_syms) | set(new_syms)):
        old_allowed = set((old_syms.get(sym) or {}).get("allowed_cells") or [])
        new_allowed = set((new_syms.get(sym) or {}).get("allowed_cells") or [])
        old_blocked = set((old_syms.get(sym) or {}).get("blocked_cells") or [])
        new_blocked = set((new_syms.get(sym) or {}).get("blocked_cells") or [])

        for cell in sorted(new_allowed - old_allowed):
            added.append({"symbol": sym, "cell": cell, "kind": "allowed"})
        for cell in sorted(old_allowed - new_allowed):
            removed.append({"symbol": sym, "cell": cell, "kind": "allowed_cleared"})
        for cell in sorted(new_blocked - old_blocked):
            added.append({"symbol": sym, "cell": cell, "kind": "blocked"})
        for cell in sorted(old_blocked - new_blocked):
            removed.append({"symbol": sym, "cell": cell, "kind": "blocked_cleared"})
    return {"added": added, "removed": removed}