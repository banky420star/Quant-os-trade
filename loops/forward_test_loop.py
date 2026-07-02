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
from core.strategy_policy import KNOWN_SETUPS, culturing_cell_from_trade  # noqa: E402
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state  # noqa: E402


def _culturing_config(config: dict[str, Any]) -> dict[str, Any]:
    cfg = config.get("culturing") or {}
    return {
        "min_n": int(cfg.get("min_n", 8)),
        "veto_win_rate_pct": float(cfg.get("veto_win_rate_pct", 40)),
        # cost_r=0 by default: live trades' realized R already includes the
        # real spread (entry/exit are filled prices), so subtracting a cost
        # model would double-charge. Set culturing.cost_r > 0 to add a
        # conservative cost burden (e.g. for paper-sourced records).
        "cost_r": float(cfg.get("cost_r", 0.0)),
    }


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
    cost_r = cconf["cost_r"]

    cells_out: dict[str, dict[str, Any]] = {}
    vetoed: dict[str, list[str]] = {}
    total_cells = 0
    total_vetoed = 0

    for symbol, cell_map in buckets.items():
        sym_cells: dict[str, Any] = {}
        sym_vetoed: list[str] = []
        for cell, recs in cell_map.items():
            stats = compute_stats(recs, cost_r=cost_r)
            loss_stats = cell_loss_stats(recs)
            n = int(stats.get("trades", 0))
            verdict = "thin"
            if n >= min_n:
                wr = float(stats.get("win_rate_pct", 0))
                exp_net = float(stats.get("expectancy_net_r", 0))
                if exp_net < 0 and wr < veto_wr:
                    verdict = "vetoed"
                    sym_vetoed.append(cell)
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
                "verdict": verdict,
            }
            total_cells += 1
        cells_out[symbol] = sym_cells
        vetoed[symbol] = sorted(sym_vetoed)
        total_vetoed += len(sym_vetoed)

    return {
        "cells": cells_out,
        "vetoed": vetoed,
        "total_cells": total_cells,
        "total_vetoed": total_vetoed,
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
    }
    write_json_state("symbol_policy_live.json", policy_payload)

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