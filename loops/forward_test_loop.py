"""Agent: Forward-test ledger — record clean per-cell outcomes, veto losers.

USER-AUTHORIZED 2026-07-01 (pure data-driven per-symbol culturing). This loop
is the "record results" half of the user's direction: bucket closed trades by
``(symbol | setup | regime_primary | align | session)`` cell, compute clean
per-cell stats, and write a veto list of cells that clean live data proves
lose. The verifier reads the veto (state/symbol_policy_live.json) and hard-
rejects candidate signals whose cell is vetoed. Cells with thin data are left
permissive — we keep collecting, never veto on noise.

This is *observation* on demo, not alpha (see VERDICT.md — no deployable edge
across 7 families at retail cost). Narrowing by clean live data is hygiene.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the evaluator's tested stats helpers — do not reinvent.
from scripts.strategy_evaluator import compute_stats  # noqa: E402
from core.blue_guardian import (
    blue_guardian_enabled,
    blue_guardian_settings,
    cell_loss_stats,
)  # noqa: E402
from core.positive_evolution import positive_evolution_active, refresh_positive_evolution  # noqa: E402
from core.strategy_policy import (  # noqa: E402
    KNOWN_SETUPS,
    culturing_cell_from_trade,
    normalize_setup_type,
)
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state  # noqa: E402


def _culturing_config(config: dict[str, Any]) -> dict[str, Any]:
    cfg = config.get("culturing") or {}
    return {
        "min_n": int(cfg.get("min_n", 8)),
        "veto_win_rate_pct": float(cfg.get("veto_win_rate_pct", 40)),
        # cost_r=0 by default: live/demo trades' realized R already includes
        # the real spread (entry/exit are FILLED prices from mt5_deal), so
        # subtracting a cost model would double-charge. This is the honest
        # reconciliation of "live cost_r=0 vs research 30bps": research applies
        # 30bps as a MODEL on theoretical mid-price entries (no fills); the
        # live ledger doesn't need to because it has fills. Set culturing.cost_r
        # > 0 to add a blanket conservative burden (applies to ALL records).
        "cost_r": float(cfg.get("cost_r", 0.0)),
        # 2026-07-31 — cost-aware veto. paper_cost_r applies ONLY to records
        # without an mt5_deal/mt5_position (paper-simulated, mid-price entries
        # whose R does NOT include the spread). Fill records keep cost_r=0
        # (spread already in the fill). Default 0.0 preserves current
        # behavior; raise it (e.g. 0.15R) to burden any paper-sourced cells.
        "paper_cost_r": float(cfg.get("paper_cost_r", 0.0)),
    }


def _record_cost_basis(trade: dict[str, Any]) -> str:
    """'fill' if the trade's R already includes spread (real/demo MT5 fill),
    'paper' if it is a simulated mid-price record whose R omits the spread."""
    if trade.get("mt5_deal") or trade.get("mt5_position"):
        return "fill"
    return "paper"


def _cell_effective_cost_r(recs: list[dict[str, Any]], cconf: dict[str, Any]) -> tuple[float, str]:
    """Per-cell cost_r + basis label. Fill-only cells keep cost_r=0 (spread
    in the fill). Any paper record pulls in the paper_cost_r burden."""
    bases = {_record_cost_basis(r) for r in recs}
    if bases == {"fill"}:
        return float(cconf["cost_r"]), "fill"
    if bases == {"paper"}:
        return max(float(cconf["cost_r"]), float(cconf["paper_cost_r"])), "paper"
    return max(float(cconf["cost_r"]), float(cconf["paper_cost_r"])), "mixed"


def _setup_aggregate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Setup-level aggregate veto config. The per-cell culturing veto fragments
    evidence across ~50 (symbol|setup|regime|align|session) cells, so a setup
    that loses in aggregate (e.g. pullback: 928 trades, -$650, 33% wr) never
    trips a per-cell veto. This gate looks at each setup's FULL sample and
    vetoes proven losers — the MT5-journal review (2026-07-31) showed pullback
    was 66% of all trades and the single biggest loss driver. min_n is high
    (100) so a setup-level call is only made on real evidence, not noise.
    """
    cfg = (config.get("culturing") or {}).get("setup_aggregate_veto") or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "min_n": int(cfg.get("min_n", 100)),
        "veto_win_rate_pct": float(cfg.get("veto_win_rate_pct", 40)),
        "veto_net_r": float(cfg.get("veto_net_r", 0.0)),
    }


def _setup_from_trade_local(trade: dict[str, Any]) -> str:
    """Normalized setup label for a closed trade (broker-truncation repaired)."""
    meta = trade.get("signal_meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    mc = trade.get("market_context") or meta.get("market_context") or {}
    if not isinstance(mc, dict):
        mc = {}
    return normalize_setup_type(
        trade.get("setup_type") or meta.get("setup_type"),
        meta=meta,
        market_context=mc,
    )


def _build_setup_aggregates(
    trades: list[dict[str, Any]],
    sa_cfg: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Aggregate closed trades by normalized setup; veto proven losers.

    Returns ({setup: {n, win_rate_pct, expectancy_r, verdict}}, [vetoed_setups]).
    A setup is vetoed when n >= min_n AND net expectancy < veto_net_r AND
    win-rate < veto_win_rate_pct — the same logic as the per-cell veto, applied
    at the coarser setup grain where the sample is large enough to trust.
    """
    if not sa_cfg["enabled"]:
        return {}, []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        if t.get("archive_polluted"):
            continue
        setup = _setup_from_trade_local(t)
        if not setup or setup == "unknown":
            continue
        buckets.setdefault(setup, []).append(t)

    out: dict[str, dict[str, Any]] = {}
    vetoed: list[str] = []
    for setup, recs in buckets.items():
        stats = compute_stats(recs, cost_r=0.0)
        n = int(stats.get("trades", 0))
        verdict = "thin"
        if n >= sa_cfg["min_n"]:
            wr = float(stats.get("win_rate_pct", 0))
            exp_net = float(stats.get("expectancy_net_r", 0))
            if exp_net < sa_cfg["veto_net_r"] and wr < sa_cfg["veto_win_rate_pct"]:
                verdict = "vetoed"
                vetoed.append(setup)
            elif exp_net > 0:
                verdict = "positive"
            else:
                verdict = "ok"
        out[setup] = {
            "n": n,
            "win_rate_pct": stats.get("win_rate_pct", 0),
            "expectancy_r": stats.get("expectancy_r", 0),
            "expectancy_net_r": stats.get("expectancy_net_r", 0),
            "ci95": stats.get("ci95", [0, 0]),
            "verdict": verdict,
        }
    return out, sorted(vetoed)


def _bucket_trades(trades: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Group clean closed trades by symbol -> cell -> [trades].

    Skips records flagged archive_polluted, with unknown setup_type, or whose
    cell key can't be built (no market_context). Historical noise never drives
    a veto.
    """
    buckets: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for t in trades:
        if t.get("archive_polluted"):
            continue
        setup = t.get("setup_type")
        if not setup or setup not in KNOWN_SETUPS:
            continue
        symbol = t.get("symbol")
        if not symbol:
            continue
        cell = culturing_cell_from_trade(t)
        if not cell:
            continue
        buckets.setdefault(symbol, {}).setdefault(cell, []).append(t)
    return buckets


def build_ledger(
    trades: list[dict[str, Any]],
    cconf: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute per-cell stats + the data-driven veto list."""
    buckets = _bucket_trades(trades)
    min_n = cconf["min_n"]
    veto_wr = cconf["veto_win_rate_pct"]

    cells_out: dict[str, dict[str, Any]] = {}
    vetoed: dict[str, list[str]] = {}
    total_cells = 0
    total_vetoed = 0

    for symbol, cell_map in buckets.items():
        sym_cells: dict[str, Any] = {}
        sym_vetoed: list[str] = []
        for cell, recs in cell_map.items():
            cost_r, cost_basis = _cell_effective_cost_r(recs, cconf)
            stats = compute_stats(recs, cost_r=cost_r)
            loss_stats = cell_loss_stats(recs)
            n = int(stats.get("trades", 0))
            verdict = "thin"
            if n >= min_n:
                wr = float(stats.get("win_rate_pct", 0))
                exp_net = float(stats.get("expectancy_net_r", 0))
                pe_active = config and positive_evolution_active(config)
                if pe_active and exp_net <= 0:
                    verdict = "vetoed"
                    sym_vetoed.append(cell)
                elif exp_net < 0 and wr < veto_wr:
                    verdict = "vetoed"
                    sym_vetoed.append(cell)
                elif exp_net > 0:
                    verdict = "positive"
                elif (
                    config
                    and blue_guardian_enabled(config)
                    and bool((config.get("blue_guardian") or {}).get("culturing_loss_veto", True))
                    and (
                        float(loss_stats.get("avg_loss_usd", 0)) <= -25
                        or float(loss_stats.get("max_loss_usd", 0)) <= -35
                        or int(loss_stats.get("breach_30_count", 0)) >= 2
                    )
                ):
                    verdict = "vetoed"
                    sym_vetoed.append(cell)
                else:
                    verdict = "ok"
            sym_cells[cell] = {
                "n": n,
                "win_rate_pct": stats.get("win_rate_pct", 0),
                "expectancy_r": stats.get("expectancy_r", 0),
                "expectancy_net_r": stats.get("expectancy_net_r", 0),
                "ci95": stats.get("ci95", [0, 0]),
                "profit_factor": stats.get("profit_factor", 0),
                "avg_loss_usd": loss_stats.get("avg_loss_usd", 0),
                "max_loss_usd": loss_stats.get("max_loss_usd", 0),
                "breach_25_count": loss_stats.get("breach_25_count", 0),
                "breach_30_count": loss_stats.get("breach_30_count", 0),
                "cost_basis": cost_basis,
                "cost_r": round(cost_r, 4),
                "verdict": verdict,
            }
            total_cells += 1
        cells_out[symbol] = sym_cells
        vetoed[symbol] = sorted(sym_vetoed)
        total_vetoed += len(sym_vetoed)

    # 2026-07-31 — setup-level aggregate veto. The per-cell veto above fragments
    # evidence across ~50 cells; a setup that loses in aggregate (pullback: 928
    # trades, 33% wr) never trips a per-cell veto. This coarser gate vetoes
    # setups that are proven losers at the full-sample grain.
    sa_cfg = _setup_aggregate_config(config) if config else {"enabled": False, "min_n": 100, "veto_win_rate_pct": 40, "veto_net_r": 0.0}
    setup_aggregates, vetoed_setups = _build_setup_aggregates(trades, sa_cfg)

    return {
        "cells": cells_out,
        "vetoed": vetoed,
        "total_cells": total_cells,
        "total_vetoed": total_vetoed,
        "setup_aggregates": setup_aggregates,
        "vetoed_setups": vetoed_setups,
    }


def run() -> dict:
    """Recompute the forward-test ledger + data-driven veto from closed trades."""
    config = load_config()
    logger = setup_logger("forward_test_loop", "forward_test_loop.log")
    cconf = _culturing_config(config)

    trades_data = read_json_state("paper_trades.json", default={"trades": []})
    trades = trades_data.get("trades", []) if isinstance(trades_data, dict) else []

    ledger = build_ledger(trades, cconf, config)
    now = utc_now_iso()
    ledger_payload = {
        "updated_at": now,
        "config": cconf,
        "total_cells": ledger["total_cells"],
        "total_vetoed": ledger["total_vetoed"],
        "cells": ledger["cells"],
        "vetoed": ledger["vetoed"],
        "setup_aggregates": ledger.get("setup_aggregates", {}),
        "vetoed_setups": ledger.get("vetoed_setups", []),
    }
    write_json_state("forward_test_ledger.json", ledger_payload)

    # The veto file the verifier reads. Per-symbol vetoed cell keys only.
    # Seed every configured symbol so the dashboard/TUI always has a policy slot
    # even before that symbol has closed trades (legacy XAU/USOIL behavior).
    configured_symbols = list((config.get("mt5") or {}).get("symbols") or [])
    policy_symbols: dict[str, dict[str, Any]] = {
        sym: {"vetoed_cells": []} for sym in configured_symbols
    }
    for sym, cells in ledger["vetoed"].items():
        policy_symbols[sym] = {"vetoed_cells": cells}

    policy_payload = {
        "updated_at": now,
        "min_n": cconf["min_n"],
        "veto_win_rate_pct": cconf["veto_win_rate_pct"],
        "symbols": policy_symbols,
        # 2026-07-31 — setup-level aggregate veto (top-level). The verifier's
        # setup_aggregate_veto check reads this list and hard-rejects any signal
        # whose setup_type is in it. Empty when no setup has enough losing sample.
        "vetoed_setups": ledger.get("vetoed_setups", []),
    }
    write_json_state("symbol_policy_live.json", policy_payload)

    if positive_evolution_active(config):
        refresh_positive_evolution(config, ledger_cells=ledger["cells"], logger=logger)

    # 2026-07-31 — shadow Thompson-sampling bandit over the culturing cells.
    # Default OFF (culturing.bandit.enabled=false). When enabled it recomputes
    # per-cell posteriors + a sampled selection score into state/culturing_bandit.json
    # — SHADOW ONLY, it never touches symbol_policy_live.json (the hard veto the
    # verifier reads). See core/culturing_bandit.py for the full rationale.
    try:
        from core.culturing_bandit import bandit_config, run as bandit_run

        if bandit_config(config)["enabled"]:
            bandit_run(config)
            logger.info("Culturing bandit: shadow posteriors refreshed -> state/culturing_bandit.json")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Culturing bandit refresh skipped: %s", exc)

    # Per-symbol ledger files under state/culturing/<symbol>.json — one
    # self-contained file per symbol so the dashboard can fetch a single
    # symbol's full cell detail on demand (drill-down) without pulling the
    # whole aggregate. The aggregate forward_test_ledger.json above stays as
    # the backward-compatible index the TUI + dashboard overview read. Symbol
    # names are alphanumeric + 'm' (no path separators) but sanitize anyway.
    bg_settings = blue_guardian_settings(config) if blue_guardian_enabled(config) else {}
    per_symbol_written = 0
    culturing_symbols = set(configured_symbols) | set(ledger["cells"].keys())
    for sym in sorted(culturing_symbols):
        sym_cells = ledger["cells"].get(sym, {})
        safe = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in sym)
        sym_vetoed = sorted(ledger["vetoed"].get(sym, []))
        caps = (bg_settings.get("per_symbol_max_lot") or {}) if bg_settings else {}
        rec_lot = float(caps[sym]) if sym in caps else None
        sym_payload = {
            "updated_at": now,
            "symbol": sym,
            "config": cconf,
            "total_cells": len(sym_cells),
            "total_vetoed": len(sym_vetoed),
            "vetoed_cells": sym_vetoed,
            "cells": sym_cells,
            "blue_guardian": {
                "recommended_max_lot": rec_lot,
                "risk_per_trade_usd": bg_settings.get("risk_per_trade_usd"),
            } if bg_settings else {},
        }
        write_json_state(f"culturing/{safe}.json", sym_payload)
        per_symbol_written += 1

    logger.info(
        "Forward-test ledger: %d cells across %d symbols, %d vetoed, %d per-symbol files (min_n=%d, veto_wr<%.0f%%, cost_r=%.3f)",
        ledger["total_cells"],
        len(ledger["cells"]),
        ledger["total_vetoed"],
        per_symbol_written,
        cconf["min_n"],
        cconf["veto_win_rate_pct"],
        cconf["cost_r"],
    )
    return ledger_payload


if __name__ == "__main__":
    # Standalone: print top/bottom cells by realized net expectancy for a
    # quick human audit of what the veto is seeing.
    cfg = load_config()
    cc = _culturing_config(cfg)
    data = read_json_state("paper_trades.json", default={"trades": []})
    ts = data.get("trades", []) if isinstance(data, dict) else []
    res = build_ledger(ts, cc, cfg)
    flat = []
    for sym, cells in res["cells"].items():
        for cell, st in cells.items():
            flat.append((sym, cell, st))
    flat.sort(key=lambda r: r[2].get("expectancy_net_r", 0))
    print(f"culturing config: {cc}")
    print(f"total cells={res['total_cells']} vetoed={res['total_vetoed']}")
    print("--- bottom 15 (candidates to veto) ---")
    for sym, cell, st in flat[:15]:
        print(f"  {sym:10} {cell:45} n={st['n']:3} wr={st['win_rate_pct']:5.1f} netR={st['expectancy_net_r']:+.3f} {st['verdict']}")
    print("--- top 15 ---")
    for sym, cell, st in flat[-15:]:
        print(f"  {sym:10} {cell:45} n={st['n']:3} wr={st['win_rate_pct']:5.1f} netR={st['expectancy_net_r']:+.3f} {st['verdict']}")