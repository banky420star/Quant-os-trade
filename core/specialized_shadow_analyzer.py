"""Shadow fire-ledger analyzer (iteration 9, 2026-08-01).

Reads ``state/specialized_shadow_ledger.jsonl`` (populated by
``core.specialized_setups._shadow_log`` when ``signals.specialized_setups.shadow``
is true) and turns the raw fire log into per-symbol / per-setup / per-session
fire-frequency aggregates with thin-cell flagging.

HONEST scope: this reports **what is firing on which symbols, how often, and in
which session** — the first readable per-symbol evidence from the no-orders
shadow pipeline. It does NOT yet report win-rate / expectancy / net-R per cell.
Turning fires into "what works per symbol" requires an offline outcome-labeler
that joins each fire to forward price history and computes a synthetic R — that
is the flagged next piece, not included here. Fire-frequency alone is still
useful: it surfaces dead detectors (never fire), over-firing setups, and cells
that will never reach the culturing n>=8 threshold so an operator can prune
them before risking even demo capital.

No orders, no live trading, no kill-switch interaction — read-only analysis.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from core.utils import STATE_DIR, write_json_state

SHADOW_LEDGER_NAME = "specialized_shadow_ledger"
SHADOW_REPORT_NAME = "specialized_shadow_report.json"
DEFAULT_MIN_N = 8  # mirror the culturing veto minimum sample size


def shadow_ledger_path(filename: str = SHADOW_LEDGER_NAME) -> Path:
    """Resolve state/<name>.jsonl (stem-only name auto-appends .jsonl)."""
    name = filename if filename.endswith(".jsonl") else f"{filename}.jsonl"
    return STATE_DIR / name


def read_shadow_ledger(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Read all fire records from the shadow ledger JSONL. Skips blank/bad lines."""
    p = Path(path) if path else shadow_ledger_path()
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def session_from_utc_hour(hour: int) -> str:
    """Map a UTC hour to a coarse session label for bucketing.

    Aligned with the specialized-setup windows so a fire's session is the
    killzone it occurred in. Hours outside any killzone -> ``off_hours``.
    """
    if not isinstance(hour, int) or hour < 0:
        return "unknown"
    if 0 <= hour < 2:
        return "tokyo"
    if 2 <= hour < 7:
        return "off_hours"
    if 7 <= hour < 10:
        return "london_open"
    if 10 <= hour < 14:
        return "london_morning"
    if 14 <= hour < 17:
        return "ny_rth"
    if 17 <= hour < 19:
        return "ny_lunch"
    if 19 <= hour < 21:
        return "ny_late"
    if 21 <= hour < 24:
        return "sydney"
    return "off_hours"


def _cell_key(symbol: str | None, setup: str | None, session: str) -> tuple[str, str, str]:
    return (symbol or "?", setup or "?", session)


def analyze_fires(records: list[dict[str, Any]], min_n: int = DEFAULT_MIN_N) -> dict[str, Any]:
    """Aggregate fire records into per-cell + per-symbol + per-setup views.

    Each "cell" is (symbol, setup_type, session) — the same dimensions the real
    culturing ledger uses, so a future outcome-labeler can join cleanly. A cell
    with n < min_n is flagged ``thin`` (not yet evaluable for win-rate verdicts).
    """
    per_cell: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "buy": 0, "sell": 0, "conf_sum": 0.0, "first": None, "last": None}
    )
    per_symbol: dict[str, int] = defaultdict(int)
    per_setup: dict[str, int] = defaultdict(int)
    per_session: dict[str, int] = defaultdict(int)
    bad = 0

    for rec in records:
        symbol = rec.get("symbol")
        setup = rec.get("setup_type")
        if not symbol or not setup:
            bad += 1
            continue
        hour = rec.get("utc_hour")
        try:
            hour_i = int(hour)
        except (TypeError, ValueError):
            hour_i = -1
        session = session_from_utc_hour(hour_i)
        cell = _cell_key(symbol, setup, session)
        c = per_cell[cell]
        c["n"] += 1
        side = rec.get("side")
        if side == "BUY":
            c["buy"] += 1
        elif side == "SELL":
            c["sell"] += 1
        try:
            c["conf_sum"] += float(rec.get("confidence") or 0.0)
        except (TypeError, ValueError):
            pass
        fired_at = rec.get("fired_at")
        if fired_at:
            if c["first"] is None or fired_at < c["first"]:
                c["first"] = fired_at
            if c["last"] is None or fired_at > c["last"]:
                c["last"] = fired_at
        per_symbol[symbol] += 1
        per_setup[setup] += 1
        per_session[session] += 1

    cells: list[dict[str, Any]] = []
    thin: list[dict[str, Any]] = []
    for (symbol, setup, session), c in sorted(per_cell.items(), key=lambda kv: -kv[1]["n"]):
        n = c["n"]
        avg_conf = round(c["conf_sum"] / n, 3) if n else 0.0
        row = {
            "symbol": symbol,
            "setup_type": setup,
            "session": session,
            "n": n,
            "buy": c["buy"],
            "sell": c["sell"],
            "avg_confidence": avg_conf,
            "first_fire": c["first"],
            "last_fire": c["last"],
            "thin": n < min_n,
        }
        cells.append(row)
        if n < min_n:
            thin.append(row)

    return {
        "total_fires": sum(per_symbol.values()),
        "valid_records": sum(per_symbol.values()),
        "skipped_bad": bad,
        "min_n": min_n,
        "distinct_cells": len(cells),
        "thin_cells": len(thin),
        "per_symbol": dict(sorted(per_symbol.items(), key=lambda kv: -kv[1])),
        "per_setup": dict(sorted(per_setup.items(), key=lambda kv: -kv[1])),
        "per_session": dict(sorted(per_session.items(), key=lambda kv: -kv[1])),
        "cells": cells,
        "thin_cells_detail": thin,
    }


def write_shadow_report(report: dict[str, Any]) -> Path:
    """Persist the aggregated report to state/specialized_shadow_report.json."""
    write_json_state(SHADOW_REPORT_NAME, report)
    return STATE_DIR / SHADOW_REPORT_NAME


def run(min_n: int = DEFAULT_MIN_N, path: Path | str | None = None) -> dict[str, Any]:
    """Read the ledger, analyze, persist the report, return it."""
    records = read_shadow_ledger(path)
    report = analyze_fires(records, min_n=min_n)
    write_shadow_report(report)
    return report