"""Strategy arena — all triggered setups compete; winners earn points.

When ``strategy_arena.emit_all_setups`` is enabled the decision engine emits
every setup the classifier fires (not just the top-confidence pick). Closed
trade outcomes update a per-setup leaderboard; the highest-scoring setup per
symbol is logged as the current leader.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from core.arena_analytics import (
    default_analytics,
    extract_conditions,
    log_insights,
    persist_insights,
    update_analytics,
)
from core.setup_library import SETUP_LIBRARY
from core.setup_triggers import SETUP_ORDER, export_catalog, write_setup_catalog
from core.utils import read_json_state, setup_logger, utc_now_iso, write_json_state

STATE_FILE = "strategy_arena.json"
LOG_NAME = "strategy_arena"


def arena_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("strategy_arena") or {}
    symbols = raw.get("symbols")
    if symbols:
        sym_list = list(symbols)
    else:
        sym_list = list(config.get("mt5", {}).get("symbols") or [])
    return {
        "enabled": bool(raw.get("enabled", False)),
        "emit_all_setups": bool(raw.get("emit_all_setups", True)),
        "log_triggers": bool(raw.get("log_triggers", True)),
        "log_outcomes": bool(raw.get("log_outcomes", True)),
        "symbols": sym_list,
        "max_open_per_symbol": max(1, int(raw.get("max_open_per_symbol", 3))),
        "max_open_per_setup": max(1, int(raw.get("max_open_per_setup", 1))),
        "points_win": float(raw.get("points_win", 10)),
        "points_loss": float(raw.get("points_loss", -4)),
        "points_per_r": float(raw.get("points_per_r", 5)),
        "min_confidence": int(raw.get("min_confidence", 0)),
    }


def arena_enabled(config: dict[str, Any]) -> bool:
    return arena_settings(config)["enabled"]


def arena_logger() -> logging.Logger:
    return setup_logger(LOG_NAME, f"{LOG_NAME}.log")


def _default_state() -> dict[str, Any]:
    return {
        "campaign_id": None,
        "started_at": None,
        "updated_at": None,
        "symbols": [],
        "setup_types": list(SETUP_ORDER),
        "setup_catalog": None,
        "analytics": default_analytics(),
        "insights": {},
        "trigger_log": [],
        "outcome_log": [],
        "leaderboard": {},
        "by_symbol": {},
        "totals": {
            "triggers": 0,
            "trades_closed": 0,
            "wins": 0,
            "losses": 0,
            "points": 0.0,
        },
    }


def load_arena() -> dict[str, Any]:
    data = read_json_state(STATE_FILE, default={})
    if not isinstance(data, dict) or not data:
        return _default_state()
    base = _default_state()
    base.update(data)
    return base


def rebuild_analytics_from_outcomes(state: dict[str, Any]) -> dict[str, Any]:
    """Recompute session/symbol/condition analytics from stored outcome_log."""
    analytics = default_analytics()
    trigger_log = state.get("trigger_log") or []
    for trade in state.get("outcome_log") or []:
        if not isinstance(trade, dict):
            continue
        pseudo = {
            "trade_id": trade.get("trade_id"),
            "symbol": trade.get("symbol"),
            "setup_type": trade.get("setup_type"),
            "side": trade.get("side"),
            "pnl": trade.get("pnl"),
            "result": trade.get("result"),
            "signal_meta": {
                "market_context": {
                    "session": trade.get("session"),
                    "move_type": trade.get("move_type"),
                    "market_regime": {"primary": trade.get("regime")},
                },
                "trigger_summary": trade.get("trigger_summary"),
            },
            "conditions": trade.get("conditions"),
        }
        pts = float(trade.get("points_awarded") or 0)
        r_mult = float(trade.get("r_multiple") or 0)
        update_analytics(analytics, pseudo, points=pts, r_mult=r_mult, trigger_log=trigger_log)
    state["analytics"] = analytics
    insights = persist_insights(state)
    save_arena(state)
    return insights


def save_arena(state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now_iso()
    write_json_state(STATE_FILE, state)


def reset_arena(
    config: dict[str, Any],
    *,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """Fresh arena campaign — wipe scores and logs, keep config symbols."""
    settings = arena_settings(config)
    now = utc_now_iso()
    cid = campaign_id or f"arena-{now[:10]}"
    state = _default_state()
    state["campaign_id"] = cid
    state["started_at"] = now
    state["symbols"] = list(settings["symbols"])
    for setup in state["setup_types"]:
        state["leaderboard"][setup] = {
            "setup_type": setup,
            "points": 0.0,
            "wins": 0,
            "losses": 0,
            "trades": 0,
            "net_pnl": 0.0,
            "expectancy_r": 0.0,
            "triggers": 0,
        }
    for sym in state["symbols"]:
        state["by_symbol"][sym] = {
            s: {
                "setup_type": s,
                "symbol": sym,
                "points": 0.0,
                "wins": 0,
                "losses": 0,
                "trades": 0,
                "net_pnl": 0.0,
                "expectancy_r": 0.0,
                "triggers": 0,
            }
            for s in state["setup_types"]
        }
    state["setup_catalog"] = export_catalog(config)
    state["analytics"] = default_analytics()
    state["insights"] = {}
    save_arena(state)
    write_setup_catalog(config)
    persist_insights(state)
    log = arena_logger()
    log.info(
        "Arena reset campaign=%s symbols=%s setups=%d catalog=%d triggers",
        cid,
        state["symbols"],
        len(state["setup_types"]),
        len(state["setup_catalog"].get("setups", [])),
    )
    return state


def _ensure_setup_bucket(state: dict[str, Any], setup_type: str) -> dict[str, Any]:
    lb = state.setdefault("leaderboard", {})
    if setup_type not in lb:
        lb[setup_type] = {
            "setup_type": setup_type,
            "points": 0.0,
            "wins": 0,
            "losses": 0,
            "trades": 0,
            "net_pnl": 0.0,
            "expectancy_r": 0.0,
            "triggers": 0,
        }
    return lb[setup_type]


def _ensure_symbol_setup(
    state: dict[str, Any],
    symbol: str,
    setup_type: str,
) -> dict[str, Any]:
    by_sym = state.setdefault("by_symbol", {})
    sym_map = by_sym.setdefault(symbol, {})
    if setup_type not in sym_map:
        sym_map[setup_type] = {
            "setup_type": setup_type,
            "symbol": symbol,
            "points": 0.0,
            "wins": 0,
            "losses": 0,
            "trades": 0,
            "net_pnl": 0.0,
            "expectancy_r": 0.0,
            "triggers": 0,
        }
    return sym_map[setup_type]


def _calc_r_multiple(trade: dict[str, Any]) -> float:
    entry = float(trade.get("entry") or 0)
    exit_p = float(trade.get("exit") or trade.get("exit_price") or entry)
    sl = float(trade.get("sl") or 0)
    side = str(trade.get("side", "BUY")).upper()
    if entry <= 0 or sl <= 0:
        pnl = float(trade.get("pnl") or 0)
        return 1.0 if pnl > 0 else (-1.0 if pnl < 0 else 0.0)
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0
    move = (exit_p - entry) if side == "BUY" else (entry - exit_p)
    return move / risk


def award_points(trade: dict[str, Any], settings: dict[str, Any]) -> float:
    """Points for a closed trade: base win/loss + R-multiple bonus."""
    r = _calc_r_multiple(trade)
    pnl = float(trade.get("pnl") or 0)
    if pnl > 0 or trade.get("result") == "win":
        return settings["points_win"] + settings["points_per_r"] * max(0.0, r)
    if pnl < 0 or trade.get("result") == "loss":
        return settings["points_loss"] + settings["points_per_r"] * min(0.0, r)
    return 0.0


def _trigger_index(trigger_log: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for row in trigger_log or []:
        sid = row.get("signal_id")
        if sid:
            idx[str(sid)] = row
    return idx


def record_triggers(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Log every arena candidate emitted this cycle."""
    settings = arena_settings(config)
    if not settings["enabled"]:
        return {}
    log = logger or arena_logger()
    state = load_arena()
    if not state.get("campaign_id"):
        state = reset_arena(config)

    now = utc_now_iso()
    entries: list[dict[str, Any]] = []
    for sig in candidates:
        if sig.get("arena_mode") is not True:
            continue
        sym = sig.get("symbol")
        setup = sig.get("setup_type")
        if sym not in settings["symbols"]:
            continue
        row = {
            "ts": now,
            "symbol": sym,
            "setup_type": setup,
            "side": sig.get("side"),
            "confidence": sig.get("confidence"),
            "signal_id": sig.get("signal_id"),
            "session": (sig.get("market_context") or {}).get("session"),
            "regime": ((sig.get("market_context") or {}).get("market_regime") or {}).get("primary"),
            "trigger_summary": sig.get("trigger_summary"),
            "trigger_context": sig.get("trigger_context"),
        }
        entries.append(row)
        bucket = _ensure_setup_bucket(state, setup)
        bucket["triggers"] = int(bucket.get("triggers", 0)) + 1
        sym_bucket = _ensure_symbol_setup(state, sym, setup)
        sym_bucket["triggers"] = int(sym_bucket.get("triggers", 0)) + 1
        state["totals"]["triggers"] = int(state["totals"].get("triggers", 0)) + 1
        if settings["log_triggers"]:
            log.info(
                "TRIGGER %s %s %s conf=%s regime=%s session=%s | %s",
                sym,
                setup,
                sig.get("side"),
                sig.get("confidence"),
                row.get("regime"),
                row.get("session"),
                sig.get("trigger_summary") or "",
            )

    if entries:
        trig_log = state.setdefault("trigger_log", [])
        trig_log.extend(entries)
        if len(trig_log) > 500:
            state["trigger_log"] = trig_log[-500:]
    save_arena(state)
    return {"recorded": len(entries), "state": state}


def record_outcomes(
    trades: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Award points when arena trades close."""
    settings = arena_settings(config)
    if not settings["enabled"] or not trades:
        return {"recorded": 0}

    log = logger or arena_logger()
    state = load_arena()
    if not state.get("campaign_id"):
        state = reset_arena(config)

    known = {e.get("trade_id") for e in state.get("outcome_log", []) if e.get("trade_id")}
    recorded = 0
    for trade in trades:
        tid = trade.get("trade_id")
        if tid and tid in known:
            continue
        setup = trade.get("setup_type")
        sym = trade.get("symbol")
        if not setup or not sym:
            continue
        pts = award_points(trade, settings)
        r_mult = round(_calc_r_multiple(trade), 3)
        pnl = float(trade.get("pnl") or 0)
        is_win = pnl > 0 or trade.get("result") == "win"

        bucket = _ensure_setup_bucket(state, setup)
        bucket["points"] = round(float(bucket.get("points", 0)) + pts, 2)
        bucket["trades"] = int(bucket.get("trades", 0)) + 1
        bucket["net_pnl"] = round(float(bucket.get("net_pnl", 0)) + pnl, 2)
        if is_win:
            bucket["wins"] = int(bucket.get("wins", 0)) + 1
        else:
            bucket["losses"] = int(bucket.get("losses", 0)) + 1
        n = bucket["trades"]
        bucket["expectancy_r"] = round(
            (float(bucket.get("expectancy_r", 0)) * (n - 1) + r_mult) / n,
            4,
        ) if n else 0.0

        sym_bucket = _ensure_symbol_setup(state, sym, setup)
        sym_bucket["points"] = round(float(sym_bucket.get("points", 0)) + pts, 2)
        sym_bucket["trades"] = int(sym_bucket.get("trades", 0)) + 1
        sym_bucket["net_pnl"] = round(float(sym_bucket.get("net_pnl", 0)) + pnl, 2)
        if is_win:
            sym_bucket["wins"] = int(sym_bucket.get("wins", 0)) + 1
        else:
            sym_bucket["losses"] = int(sym_bucket.get("losses", 0)) + 1

        state["totals"]["trades_closed"] = int(state["totals"].get("trades_closed", 0)) + 1
        state["totals"]["points"] = round(float(state["totals"].get("points", 0)) + pts, 2)
        if is_win:
            state["totals"]["wins"] = int(state["totals"].get("wins", 0)) + 1
        else:
            state["totals"]["losses"] = int(state["totals"].get("losses", 0)) + 1

        cond = extract_conditions(trade, _trigger_index(state.get("trigger_log", [])))
        row = {
            "ts": utc_now_iso(),
            "trade_id": tid,
            "symbol": sym,
            "setup_type": setup,
            "side": trade.get("side"),
            "pnl": pnl,
            "r_multiple": r_mult,
            "points_awarded": round(pts, 2),
            "result": "win" if is_win else "loss",
            "session": cond.get("session"),
            "regime": cond.get("regime"),
            "move_type": cond.get("move_type"),
            "m5_trend": cond.get("m5_trend"),
            "trigger_summary": cond.get("trigger_summary"),
            "conditions": cond,
        }
        state.setdefault("outcome_log", []).append(row)
        update_analytics(
            state.setdefault("analytics", default_analytics()),
            trade,
            points=pts,
            r_mult=r_mult,
            trigger_log=state.get("trigger_log"),
        )
        recorded += 1
        if settings["log_outcomes"]:
            log.info(
                "OUTCOME %s %s %s pnl=%.2f R=%.2f pts=%+.1f (total=%.1f)",
                sym,
                setup,
                row["result"],
                pnl,
                r_mult,
                pts,
                bucket["points"],
            )

    if recorded:
        outcome_log = state.get("outcome_log", [])
        if len(outcome_log) > 500:
            state["outcome_log"] = outcome_log[-500:]
        insights = persist_insights(state)
        log_insights(log, insights)
        leaders = leaderboard(state)
        for sym, leader in leaders.get("by_symbol", {}).items():
            if leader:
                log.info(
                    "LEADER %s -> %s (%.1f pts, %d trades, expR=%.2f)",
                    sym,
                    leader.get("setup_type"),
                    leader.get("points", 0),
                    leader.get("trades", 0),
                    leader.get("expectancy_r", 0),
                )
        top = leaders.get("global_top")
        if top:
            log.info(
                "GLOBAL LEADER %s (%.1f pts, wr=%.0f%%, %d trades)",
                top.get("setup_type"),
                top.get("points", 0),
                top.get("win_rate_pct", 0),
                top.get("trades", 0),
            )
    save_arena(state)
    return {"recorded": recorded, "state": state, "leaders": leaderboard(state)}


def leaderboard(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rank setups by points globally and per symbol."""
    state = state or load_arena()
    lb = state.get("leaderboard", {})
    global_rows = []
    for setup, row in lb.items():
        trades = int(row.get("trades", 0))
        wins = int(row.get("wins", 0))
        wr = (100.0 * wins / trades) if trades else 0.0
        global_rows.append({**row, "win_rate_pct": round(wr, 1)})
    global_rows.sort(
        key=lambda r: (
            -float(r.get("points", 0)),
            -float(r.get("expectancy_r", 0)),
            -int(r.get("trades", 0)),
        ),
    )
    for i, row in enumerate(global_rows):
        row["rank"] = i + 1

    by_symbol: dict[str, dict[str, Any] | None] = {}
    for sym, setups in (state.get("by_symbol") or {}).items():
        rows = []
        for _setup, row in setups.items():
            trades = int(row.get("trades", 0))
            wins = int(row.get("wins", 0))
            wr = (100.0 * wins / trades) if trades else 0.0
            rows.append({**row, "win_rate_pct": round(wr, 1)})
        rows.sort(
            key=lambda r: (
                -float(r.get("points", 0)),
                -float(r.get("expectancy_r", 0)),
                -int(r.get("triggers", 0)),
            ),
        )
        for i, row in enumerate(rows):
            row["rank"] = i + 1
        by_symbol[sym] = rows[0] if rows else None

    return {
        "campaign_id": state.get("campaign_id"),
        "updated_at": state.get("updated_at"),
        "global": global_rows,
        "global_top": global_rows[0] if global_rows else None,
        "by_symbol": by_symbol,
        "totals": state.get("totals", {}),
        "insights": state.get("insights", {}),
        "analytics": state.get("analytics", {}),
    }


def setup_capacity_available(
    config: dict[str, Any],
    symbol: str,
    setup_type: str,
    active_positions: list[dict[str, Any]],
) -> bool:
    """Arena: allow one open position per (symbol, setup_type) pair."""
    if not arena_enabled(config):
        return True
    settings = arena_settings(config)
    limit = settings["max_open_per_setup"]
    count = sum(
        1 for p in active_positions
        if p.get("symbol") == symbol and p.get("setup_type") == setup_type
    )
    return count < limit


def sync_arena_symbols(config: dict[str, Any]) -> dict[str, Any]:
    """Copy arena symbol list into mt5.symbols and practice.symbols when active."""
    if not arena_enabled(config):
        return config
    settings = arena_settings(config)
    symbols = settings["symbols"]
    if not symbols:
        return config
    config.setdefault("mt5", {})["symbols"] = list(symbols)
    config.setdefault("practice", {})["symbols"] = list(symbols)
    per_sym = settings.get("max_open_per_symbol")
    if per_sym:
        config.setdefault("trading", {})["max_open_per_symbol"] = int(per_sym)
    per_setup = settings.get("max_open_per_setup")
    if per_setup:
        config.setdefault("strategy_arena", {})["max_open_per_setup"] = int(per_setup)
    return config