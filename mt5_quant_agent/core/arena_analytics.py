"""Arena analytics — which setups win in which session/symbol/conditions."""

from __future__ import annotations

import logging
from typing import Any

from core.strategy_policy import normalize_setup_type
from core.utils import utc_now_iso, write_json_state


def _nested_bucket(root: dict, *keys: str) -> dict[str, Any]:
    cur = root
    for key in keys:
        cur = cur.setdefault(key, {})
    return cur


def _empty_bucket() -> dict[str, Any]:
    return {
        "wins": 0,
        "losses": 0,
        "trades": 0,
        "net_pnl": 0.0,
        "points": 0.0,
        "expectancy_r": 0.0,
        "sum_r": 0.0,
    }


def _bump_bucket(bucket: dict[str, Any], *, pnl: float, points: float, r_mult: float, is_win: bool) -> None:
    bucket["trades"] = int(bucket.get("trades", 0)) + 1
    bucket["net_pnl"] = round(float(bucket.get("net_pnl", 0)) + pnl, 2)
    bucket["points"] = round(float(bucket.get("points", 0)) + points, 2)
    bucket["sum_r"] = round(float(bucket.get("sum_r", 0)) + r_mult, 4)
    n = bucket["trades"]
    bucket["expectancy_r"] = round(float(bucket.get("sum_r", 0)) / n, 4) if n else 0.0
    if is_win:
        bucket["wins"] = int(bucket.get("wins", 0)) + 1
    else:
        bucket["losses"] = int(bucket.get("losses", 0)) + 1
    wr = (100.0 * bucket["wins"] / n) if n else 0.0
    bucket["win_rate_pct"] = round(wr, 1)


def extract_conditions(trade: dict[str, Any], trigger_lookup: dict[str, dict] | None = None) -> dict[str, Any]:
    """Pull session/regime/structure context from a closed trade."""
    meta = trade.get("signal_meta") or {}
    mctx = meta.get("market_context") or trade.get("market_context") or {}
    if not isinstance(mctx, dict):
        mctx = {}
    regime_block = mctx.get("market_regime") or {}
    if not isinstance(regime_block, dict):
        regime_block = {}

    setup = normalize_setup_type(
        trade.get("setup_type") or meta.get("setup_type"),
        meta=meta,
        market_context=mctx,
    )
    signal_id = trade.get("signal_id") or meta.get("signal_id")
    trig = (trigger_lookup or {}).get(str(signal_id or ""), {})

    evidence = trade.get("evidence") or meta.get("evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}

    trigger_ctx = (
        trade.get("trigger_context")
        or meta.get("trigger_context")
        or trig.get("trigger_context")
        or {}
    )
    feat_ctx = trigger_ctx.get("feat") if isinstance(trigger_ctx.get("feat"), dict) else {}

    return {
        "symbol": trade.get("symbol"),
        "setup_type": setup,
        "side": trade.get("side") or meta.get("side"),
        "session": mctx.get("session") or trig.get("session") or "unknown",
        "regime": regime_block.get("primary") or mctx.get("regime") or trig.get("regime") or "unknown",
        "phase": mctx.get("phase") or (trigger_ctx.get("ctx") or {}).get("phase"),
        "move_type": mctx.get("move_type") or (trigger_ctx.get("ctx") or {}).get("move_type"),
        "market_intent": mctx.get("market_intent"),
        "m5_trend": feat_ctx.get("m5_trend"),
        "breakout": feat_ctx.get("breakout"),
        "bb_position": feat_ctx.get("bb_position"),
        "rejection": feat_ctx.get("rejection"),
        "confidence": trade.get("confidence") or meta.get("confidence"),
        "trigger_summary": (
            trade.get("trigger_summary")
            or meta.get("trigger_summary")
            or trig.get("trigger_summary")
        ),
        "trigger_context": trigger_ctx,
        "evidence": evidence,
        "signal_id": signal_id,
    }


def condition_cell_key(cond: dict[str, Any]) -> str:
    """Compact key for grouping wins by the conditions that caused them."""
    parts = [
        cond.get("symbol") or "?",
        cond.get("setup_type") or "?",
        cond.get("session") or "?",
        cond.get("regime") or "?",
        cond.get("move_type") or "-",
        cond.get("m5_trend") or "-",
    ]
    return "|".join(str(p) for p in parts)


def _trigger_index(trigger_log: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for row in trigger_log or []:
        sid = row.get("signal_id")
        if sid:
            idx[str(sid)] = row
    return idx


def default_analytics() -> dict[str, Any]:
    return {
        "by_setup_session": {},
        "by_symbol_setup": {},
        "by_symbol_session": {},
        "condition_cells": {},
        "updated_at": None,
    }


def update_analytics(
    analytics: dict[str, Any],
    trade: dict[str, Any],
    *,
    points: float,
    r_mult: float,
    trigger_log: list[dict] | None = None,
) -> dict[str, Any]:
    """Increment session/symbol/condition buckets for one closed trade."""
    lookup = _trigger_index(trigger_log or [])
    cond = extract_conditions(trade, lookup)
    sym = cond.get("symbol")
    setup = cond.get("setup_type")
    session = cond.get("session") or "unknown"
    if not sym or not setup:
        return analytics

    pnl = float(trade.get("pnl") or 0)
    is_win = pnl > 0 or trade.get("result") == "win"

    ss = _nested_bucket(analytics.setdefault("by_setup_session", {}), setup, session)
    if not ss.get("trades"):
        ss.update(_empty_bucket())
        ss["setup_type"] = setup
        ss["session"] = session
    _bump_bucket(ss, pnl=pnl, points=points, r_mult=r_mult, is_win=is_win)

    sym_setup = _nested_bucket(analytics.setdefault("by_symbol_setup", {}), sym, setup)
    if not sym_setup.get("trades"):
        sym_setup.update(_empty_bucket())
        sym_setup["symbol"] = sym
        sym_setup["setup_type"] = setup
    _bump_bucket(sym_setup, pnl=pnl, points=points, r_mult=r_mult, is_win=is_win)

    sym_sess = _nested_bucket(analytics.setdefault("by_symbol_session", {}), sym, session)
    if not sym_sess.get("trades"):
        sym_sess.update(_empty_bucket())
        sym_sess["symbol"] = sym
        sym_sess["session"] = session
    _bump_bucket(sym_sess, pnl=pnl, points=points, r_mult=r_mult, is_win=is_win)

    cell_key = condition_cell_key(cond)
    cell = analytics.setdefault("condition_cells", {}).setdefault(cell_key, _empty_bucket())
    if cell["trades"] == 0:
        cell.update({
            "cell_key": cell_key,
            "symbol": sym,
            "setup_type": setup,
            "session": session,
            "regime": cond.get("regime"),
            "move_type": cond.get("move_type"),
            "m5_trend": cond.get("m5_trend"),
            "trigger_summary": cond.get("trigger_summary"),
            "sample_evidence": cond.get("evidence"),
        })
    _bump_bucket(cell, pnl=pnl, points=points, r_mult=r_mult, is_win=is_win)
    analytics["updated_at"] = utc_now_iso()
    return analytics


def _rank_rows(rows: list[dict], min_trades: int = 1) -> list[dict]:
    eligible = [r for r in rows if int(r.get("trades", 0)) >= min_trades]
    eligible.sort(
        key=lambda r: (
            -float(r.get("points", 0)),
            -float(r.get("net_pnl", 0)),
            -float(r.get("expectancy_r", 0)),
            -int(r.get("trades", 0)),
        ),
    )
    for i, row in enumerate(eligible):
        row["rank"] = i + 1
    return eligible


def build_insights(analytics: dict[str, Any], *, min_trades: int = 1) -> dict[str, Any]:
    """Derive readable rankings: best setup per session/symbol, top conditions."""
    by_setup_session = analytics.get("by_setup_session") or {}
    best_setup_per_session: list[dict[str, Any]] = []
    for setup, sessions in by_setup_session.items():
        if not isinstance(sessions, dict):
            continue
        rows = [v for v in sessions.values() if isinstance(v, dict)]
        ranked = _rank_rows(rows, min_trades=min_trades)
        if ranked:
            top = dict(ranked[0])
            top["setup_type"] = setup
            best_setup_per_session.append(top)
    best_setup_per_session.sort(key=lambda r: -float(r.get("points", 0)))

    by_symbol_setup = analytics.get("by_symbol_setup") or {}
    best_setup_per_symbol: list[dict[str, Any]] = []
    for sym, setups in by_symbol_setup.items():
        if not isinstance(setups, dict):
            continue
        rows = [v for v in setups.values() if isinstance(v, dict)]
        ranked = _rank_rows(rows, min_trades=min_trades)
        if ranked:
            top = dict(ranked[0])
            top["symbol"] = sym
            best_setup_per_symbol.append(top)
    best_setup_per_symbol.sort(key=lambda r: -float(r.get("points", 0)))

    by_symbol_session = analytics.get("by_symbol_session") or {}
    best_session_per_symbol: list[dict[str, Any]] = []
    for sym, sessions in by_symbol_session.items():
        if not isinstance(sessions, dict):
            continue
        rows = [v for v in sessions.values() if isinstance(v, dict)]
        ranked = _rank_rows(rows, min_trades=min_trades)
        if ranked:
            top = dict(ranked[0])
            top["symbol"] = sym
            best_session_per_symbol.append(top)
    best_session_per_symbol.sort(key=lambda r: -float(r.get("points", 0)))

    cells = list((analytics.get("condition_cells") or {}).values())
    winning = [c for c in cells if isinstance(c, dict) and int(c.get("wins", 0)) > 0]
    top_winning_conditions = _rank_rows(winning, min_trades=min_trades)[:12]

    return {
        "updated_at": analytics.get("updated_at"),
        "best_setup_per_session": best_setup_per_session[:8],
        "best_setup_per_symbol": best_setup_per_symbol[:8],
        "best_session_per_symbol": best_session_per_symbol[:8],
        "top_winning_conditions": top_winning_conditions,
    }


def persist_insights(state: dict[str, Any]) -> dict[str, Any]:
    """Write insights into arena state and arena_insights.json."""
    analytics = state.get("analytics") or default_analytics()
    insights = build_insights(analytics)
    state["analytics"] = analytics
    state["insights"] = insights
    write_json_state("arena_insights.json", {
        "campaign_id": state.get("campaign_id"),
        "updated_at": utc_now_iso(),
        "analytics": analytics,
        "insights": insights,
    })
    return insights


def log_insights(logger: logging.Logger, insights: dict[str, Any]) -> None:
    for row in insights.get("best_setup_per_session", [])[:4]:
        logger.info(
            "INSIGHT session=%s best_setup=%s pts=%.1f wr=%.0f%% trades=%d pnl=%.2f",
            row.get("session"),
            row.get("setup_type"),
            float(row.get("points", 0)),
            float(row.get("win_rate_pct", 0)),
            int(row.get("trades", 0)),
            float(row.get("net_pnl", 0)),
        )
    for row in insights.get("best_setup_per_symbol", [])[:4]:
        logger.info(
            "INSIGHT symbol=%s best_setup=%s pts=%.1f wr=%.0f%% trades=%d",
            row.get("symbol"),
            row.get("setup_type"),
            float(row.get("points", 0)),
            float(row.get("win_rate_pct", 0)),
            int(row.get("trades", 0)),
        )
    for row in insights.get("top_winning_conditions", [])[:3]:
        logger.info(
            "INSIGHT condition %s %s session=%s regime=%s move=%s trend=%s "
            "pts=%.1f wr=%.0f%% | %s",
            row.get("symbol"),
            row.get("setup_type"),
            row.get("session"),
            row.get("regime"),
            row.get("move_type"),
            row.get("m5_trend"),
            float(row.get("points", 0)),
            float(row.get("win_rate_pct", 0)),
            row.get("trigger_summary") or "",
        )