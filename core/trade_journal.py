"""Comprehensive trade journal — conditions, organization, per-trade detail files."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from core.strategy_policy import culturing_cell_key, culturing_cell_from_trade


_FEATURE_KEYS = (
    "price", "m5_trend", "m15_trend", "atr", "atr_ratio", "volatility_regime",
    "volume_ratio", "spread_points", "bb_position", "stoch_k", "stoch_d",
    "stoch_cross", "breakout", "rejection", "support", "resistance",
)


def snapshot_signal_meta(
    signal: dict[str, Any],
    *,
    features_at_entry: dict[str, Any] | None = None,
    kelly: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full signal context stamped on the opening order (survives to trade log)."""
    mc = signal.get("market_context") if isinstance(signal.get("market_context"), dict) else {}
    reg = mc.get("market_regime") if isinstance(mc.get("market_regime"), dict) else {}
    feat = features_at_entry or {}
    trade_score = signal.get("trade_score") if isinstance(signal.get("trade_score"), dict) else {}
    rank = signal.get("strategy_rank") if isinstance(signal.get("strategy_rank"), dict) else {}
    return {
        "signal_id": signal.get("signal_id"),
        # Optional, explicit tag for paper-only adaptive validation. It is never
        # inferred from generic proposal IDs, preventing cross-experiment mixing.
        "adaptive_symbol_proposal_id": signal.get("adaptive_symbol_proposal_id"),
        "symbol": signal.get("symbol"),
        "side": signal.get("side"),
        "setup_type": signal.get("setup_type"),
        "entry": signal.get("entry"),
        "sl": signal.get("sl"),
        "tp1": signal.get("tp1"),
        "tp2": signal.get("tp2"),
        "confidence": signal.get("confidence"),
        "confidence_tree": signal.get("confidence_tree"),
        "evidence": signal.get("evidence"),
        "market_context": mc,
        "reason": signal.get("reason"),
        "strategy_rank": rank,
        "trade_score": trade_score,
        "consensus_passed": signal.get("consensus_passed"),
        "consensus_vetoes": signal.get("consensus_vetoes"),
        "entry_narrative": signal.get("entry_narrative"),
        "explain": signal.get("explain"),
        "kelly": kelly,
        "features_at_entry": {k: feat[k] for k in _FEATURE_KEYS if k in feat},
        "regime_primary": reg.get("primary"),
        "regime_bias": reg.get("bias"),
        "session": mc.get("session"),
        "phase": mc.get("phase"),
        "move_type": mc.get("move_type"),
        "trigger_summary": signal.get("trigger_summary"),
        "trigger_context": signal.get("trigger_context"),
        "management_profile": signal.get("management_profile"),
        "execution_policy": signal.get("execution_policy"),
        "evaluation": signal.get("evaluation"),
        "entry_quality": signal.get("entry_quality"),
    }


def safe_journal_name(trade_id: str | None) -> str:
    raw = str(trade_id or "unknown")
    return re.sub(r"[^\w\-.]", "_", raw)[:120]


def safe_symbol_name(symbol: str | None) -> str:
    """Filesystem-safe symbol for per-symbol journal paths."""
    raw = str(symbol or "unknown")
    return re.sub(r"[^\w\-.]", "_", raw)[:40]


def build_mgmt_narratives(
    *,
    side: str,
    entry: float,
    sl: float | None,
    mgmt_row: dict[str, Any],
) -> dict[str, str | None]:
    """Plain-English BE/trail lines from position_management.json row."""
    be = trail = None
    if mgmt_row.get("break_even"):
        lock = abs(float(sl or entry) - float(entry))
        be = (
            f"Break-even armed: SL moved to {float(sl or entry):.5f} "
            f"(locked {lock:.5f} over entry {float(entry):.5f})."
        )
    if mgmt_row.get("trailing"):
        peak = mgmt_row.get("peak_price")
        if peak is not None and sl is not None:
            dist = abs(float(peak) - float(sl))
            trail = (
                f"Trailing active: SL at {float(sl):.5f} "
                f"({dist:.5f} behind peak {float(peak):.5f})."
            )
        else:
            trail = "Trailing stop was active at close."
    return {"be": be, "trail": trail}


def build_conditions(
    *,
    trade: dict[str, Any],
    order: dict[str, Any] | None,
    meta: dict[str, Any],
) -> dict[str, Any]:
    """Structured entry/exit trading conditions for one closed trade."""
    mc = meta.get("market_context") if isinstance(meta.get("market_context"), dict) else {}
    reg = mc.get("market_regime") if isinstance(mc.get("market_regime"), dict) else {}
    rank = meta.get("strategy_rank") if isinstance(meta.get("strategy_rank"), dict) else {}
    ts = meta.get("trade_score") if isinstance(meta.get("trade_score"), dict) else {}
    feat = meta.get("features_at_entry") if isinstance(meta.get("features_at_entry"), dict) else {}

    side = trade.get("side") or meta.get("side")
    setup = trade.get("setup_type") or meta.get("setup_type")
    cell = culturing_cell_key(
        setup,
        reg.get("primary") or meta.get("regime_primary"),
        reg.get("bias") or meta.get("regime_bias"),
        side,
        mc.get("session") or meta.get("session"),
    )

    be_narr = trade.get("be_narrative") or meta.get("be_narrative")
    trail_narr = trade.get("trail_narrative") or meta.get("trail_narrative")
    exit_narr = trade.get("exit_narrative") or meta.get("exit_narrative")

    symbol = trade.get("symbol") or meta.get("symbol")

    return {
        "symbol": symbol,
        "signal_id": meta.get("signal_id") or trade.get("signal_id"),
        "culturing_cell": cell,
        "symbol_cell": f"{symbol}|{cell}" if symbol else cell,
        "risk": {
            "volume": trade.get("volume"),
            "sl_initial": trade.get("sl_initial") or meta.get("sl"),
            "sl_at_close": trade.get("sl"),
            "risk_price": trade.get("risk_price"),
            "risk_amount": trade.get("risk_amount"),
            "tp1": trade.get("tp1") or meta.get("tp1"),
            "tp2": trade.get("tp2") or meta.get("tp2"),
        },
        "entry": {
            "opened_at": trade.get("opened_at") or (order or {}).get("created_at"),
            "order_id": (order or {}).get("order_id"),
            "fill_price": (order or {}).get("fill_price"),
            "session": mc.get("session") or meta.get("session"),
            "phase": mc.get("phase") or meta.get("phase"),
            "move_type": mc.get("move_type") or meta.get("move_type"),
            "market_intent": mc.get("market_intent"),
            "regime": {
                "primary": reg.get("primary") or meta.get("regime_primary"),
                "bias": reg.get("bias") or meta.get("regime_bias"),
                "description": reg.get("description"),
                "tags": reg.get("tags"),
            },
            "features": feat,
            "confidence": meta.get("confidence") or trade.get("confidence"),
            "confidence_tree": meta.get("confidence_tree") or trade.get("confidence_tree"),
            "evidence": meta.get("evidence") or trade.get("evidence"),
            "trade_score": {
                "total": ts.get("total"),
                "passed": ts.get("passed"),
                "threshold": ts.get("threshold"),
                "components": ts.get("components"),
                "session_detail": ts.get("session_detail"),
            },
            "strategy_rank": {
                "allowed": rank.get("allowed"),
                "reason": rank.get("reason"),
                "rank": rank.get("rank"),
                "score": rank.get("score"),
                "top_setup": (rank.get("rankings") or [{}])[0].get("setup_type") if rank.get("rankings") else None,
            },
            "consensus": {
                "passed": meta.get("consensus_passed"),
                "vetoes": meta.get("consensus_vetoes"),
            },
            "kelly": meta.get("kelly"),
            "entry_narrative": meta.get("entry_narrative"),
            "explain": meta.get("explain"),
            "reason": meta.get("reason") or trade.get("reason"),
        },
        "exit": {
            "closed_at": trade.get("closed_at"),
            "exit_price": trade.get("exit"),
            "exit_reason": trade.get("exit_reason"),
            "narratives": {
                "be": be_narr,
                "trail": trail_narr,
                "exit": exit_narr,
            },
            "management": {
                "be_triggered": trade.get("be_triggered") or meta.get("be_triggered"),
                "trail_active": trade.get("trail_active") or meta.get("trail_active"),
                "peak_price": (trade.get("position_mgmt") or {}).get("peak_price"),
            },
        },
    }


def _trade_row(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_id": t.get("trade_id"),
        "pnl": t.get("pnl"),
        "r_multiple": t.get("r_multiple"),
        "won": t.get("won"),
        "closed_at": t.get("closed_at"),
        "setup": t.get("setup") or t.get("setup_type"),
        "session": t.get("session"),
        "regime_primary": t.get("regime_primary"),
    }


def _summarize_rows(items: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(items)
    wins = sum(1 for i in items if i.get("won"))
    pnl = sum(float(i.get("pnl") or 0) for i in items)
    rs = [float(i["r_multiple"]) for i in items if i.get("r_multiple") is not None]
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate_pct": round(100.0 * wins / max(n, 1), 1),
        "total_pnl": round(pnl, 2),
        "avg_R": round(sum(rs) / len(rs), 3) if rs else None,
    }


def _organize_within_symbol(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll-up stats for ONE symbol — setup/session/regime/cell never cross symbols."""
    buckets: dict[str, dict[str, list[dict]]] = {
        "by_setup": defaultdict(list),
        "by_session": defaultdict(list),
        "by_regime": defaultdict(list),
        "by_cell": defaultdict(list),
        "by_result": defaultdict(list),
    }
    for t in trades:
        if t.get("archive_polluted"):
            continue
        row = _trade_row(t)
        setup = str(t.get("setup") or t.get("setup_type") or "unknown")
        session = str(t.get("session") or "unknown")
        regime = str(t.get("regime_primary") or "unknown")
        cell = (t.get("conditions") or {}).get("symbol_cell") or (t.get("conditions") or {}).get("culturing_cell") or "unknown"
        result = str(t.get("result") or "unknown")
        buckets["by_setup"][setup].append(row)
        buckets["by_session"][session].append(row)
        buckets["by_regime"][regime].append(row)
        buckets["by_cell"][cell].append(row)
        buckets["by_result"][result].append(row)

    out: dict[str, Any] = {}
    for key, groups in buckets.items():
        out[key] = {
            name: {**_summarize_rows(rows), "trade_ids": [r["trade_id"] for r in rows[:50]]}
            for name, rows in sorted(groups.items(), key=lambda x: (-len(x[1]), x[0]))
        }
    return out


def group_trades_by_symbol(trades: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Partition closed trades by symbol (each symbol is its own universe)."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        if t.get("archive_polluted"):
            continue
        groups[str(t.get("symbol") or "unknown")].append(t)
    return dict(groups)


def build_symbol_journal_payload(symbol: str, trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Self-contained journal ledger for one symbol."""
    clean = [t for t in trades if not t.get("archive_polluted")]
    clean.sort(key=lambda r: str(r.get("closed_at") or ""), reverse=True)
    return {
        "symbol": symbol,
        "summary": _summarize_rows(clean),
        "organized": _organize_within_symbol(clean),
        "trade_ids": [t.get("trade_id") for t in clean if t.get("trade_id")],
        "recent_trades": clean[:20],
    }


def build_organized_index(
    trades: list[dict[str, Any]],
    *,
    configured_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Per-symbol roll-ups — each symbol has its own setup/session/regime/cell stats.

    Global cross-symbol buckets are intentionally omitted: every symbol is treated
    as an individual pipeline (same pattern as culturing/<symbol>.json).
    """
    groups = group_trades_by_symbol(trades)
    symbols = sorted(set(configured_symbols or []) | set(groups.keys()))
    per_symbol: dict[str, Any] = {}
    by_symbol: dict[str, Any] = {}

    for sym in symbols:
        sym_trades = groups.get(sym, [])
        payload = build_symbol_journal_payload(sym, sym_trades)
        per_symbol[sym] = payload
        by_symbol[sym] = {
            **payload["summary"],
            "trade_ids": payload["trade_ids"][:50],
        }

    return {
        "by_symbol": by_symbol,
        "per_symbol": per_symbol,
        "symbols": symbols,
    }


def trade_detail_payload(rec: dict[str, Any]) -> dict[str, Any]:
    """Standard per-trade journal file body."""
    return {
        "trade_id": rec.get("trade_id"),
        "symbol": rec.get("symbol"),
        "summary": {k: rec.get(k) for k in (
            "symbol", "side", "setup", "result", "pnl", "r_multiple",
            "opened_at", "closed_at", "hold_human", "confidence",
        )},
        "conditions": rec.get("conditions"),
        "record": rec,
    }


def enrich_trade_record(
    rec: dict[str, Any],
    *,
    order: dict[str, Any] | None,
    raw_trade: dict[str, Any],
) -> dict[str, Any]:
    """Attach conditions block and culturing cell to a trade_log row."""
    meta = (order or {}).get("signal_meta") or raw_trade.get("signal_meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    conditions = build_conditions(trade=rec, order=order, meta=meta)
    rec["conditions"] = conditions
    rec["culturing_cell"] = conditions.get("culturing_cell")
    rec["trade_score_total"] = (conditions.get("entry") or {}).get("trade_score", {}).get("total")
    rec["rank_reason"] = (conditions.get("entry") or {}).get("strategy_rank", {}).get("reason")
    rec["entry_narrative"] = (conditions.get("entry") or {}).get("entry_narrative")
    rec["confidence_tree"] = (conditions.get("entry") or {}).get("confidence_tree")
    rec["features_at_entry"] = (conditions.get("entry") or {}).get("features")
    narr = (conditions.get("exit") or {}).get("narratives") or {}
    rec["be_narrative"] = narr.get("be")
    rec["trail_narrative"] = narr.get("trail")
    rec["exit_narrative"] = narr.get("exit")
    if not rec.get("culturing_cell"):
        rec["culturing_cell"] = culturing_cell_from_trade(raw_trade)
    return rec