"""Desktop dashboard API for MT5 Quant OS."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.equity_tracker import build_equity_curve
from core.account_mode import runtime_mode_summary
from core.remote_access import remote_access_info
from core.utils import load_config, read_json_state, utc_now_iso, write_json_state

STATE_FILES = (
    "health.json",
    "account.json",
    "risk_state.json",
    "kill_switch.json",
    "candidate_signals.json",
    "approved_signals.json",
    "rejected_signals.json",
    "paper_positions.json",
    "paper_orders.json",
    "paper_trades.json",
    "memory.json",
    "edge_scores.json",
    "market_context.json",
    "features.json",
    "replay_results.json",
    "optimizer_results.json",
    "equity_history.json",
    "edge_database.json",
    "strategy_rankings.json",
    "research_report.json",
    "research_validation.json",
    "weight_candidates.json",
    "adaptive_weights.json",
    "supervisor.json",
    "replay_job.json",
)

# Mobile / Tailscale: skip multi-MB blobs on the default dashboard poll.
LITE_SKIP_STATE = frozenset({
    "edge_database.json",
    "memory.json",
    "equity_history.json",
    "optimizer_results.json",
    "paper_orders.json",
    "replay_results.json",
    "latest_candles.json",
})


def _slim_state_file(name: str, data: dict) -> dict:
    """Trim heavy state files for phone-friendly API responses."""
    if name == "paper_orders.json":
        orders = data.get("orders") or []
        return {
            **{k: v for k, v in data.items() if k != "orders"},
            "orders": orders[-8:],
            "order_count": len(orders),
        }
    if name == "paper_trades.json":
        trades = data.get("trades") or []
        return {**data, "trades": trades[-40:], "trade_count": len(trades)}
    if name == "features.json":
        symbols = data.get("symbols") or {}
        slim_symbols = {}
        for sym, feat in symbols.items():
            slim_symbols[sym] = {k: v for k, v in feat.items() if k != "candles"}
        return {**data, "symbols": slim_symbols}
    return data

HTML_PATH = Path(__file__).resolve().parent / "index.html"
LOG_PATH = ROOT / "logs" / "system.log"


def _symbol_pnl_totals(
    symbol: str,
    positions: list[dict],
    trades: list[dict],
) -> dict[str, float | int | str | None]:
    """Aggregate realized + unrealized PnL for one symbol across all trades/positions."""
    sym_positions = [p for p in positions if p.get("symbol") == symbol]
    sym_trades = [t for t in trades if t.get("symbol") == symbol]

    unrealized = round(sum(float(p.get("profit", 0)) for p in sym_positions), 2)
    realized = round(sum(float(t.get("pnl", 0)) for t in sym_trades), 2)
    total = round(unrealized + realized, 2)

    buy_count = sum(1 for p in sym_positions if p.get("side") == "BUY")
    sell_count = sum(1 for p in sym_positions if p.get("side") == "SELL")
    side_parts = []
    if buy_count:
        side_parts.append(f"{buy_count}× BUY")
    if sell_count:
        side_parts.append(f"{sell_count}× SELL")
    position_summary = " · ".join(side_parts) if side_parts else None

    wins = sum(1 for t in sym_trades if t.get("result") == "win")
    losses = len(sym_trades) - wins

    return {
        "total_pnl": total,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "open_position_count": len(sym_positions),
        "closed_trade_count": len(sym_trades),
        "closed_wins": wins,
        "closed_losses": losses,
        "position_summary": position_summary,
        "has_trading_activity": bool(sym_positions or sym_trades),
    }


def _build_symbol_cards(
    features: dict,
    market_ctx: dict,
    positions: dict,
    paper_trades: dict | None = None,
) -> list[dict]:
    """Build one card per symbol from features.json schema — no signals required."""
    feat_symbols = features.get("symbols", {})
    if not feat_symbols:
        return []

    ctx_root = market_ctx.get("market_context", market_ctx)
    ctx_symbols = ctx_root.get("symbols", {})
    open_positions = list((positions or {}).get("positions", []))
    closed_trades = list((paper_trades or {}).get("trades", []))

    cards = []
    for sym, feat in feat_symbols.items():
        ctx = ctx_symbols.get(sym, {})
        regime = ctx.get("market_regime", {})
        pnl = _symbol_pnl_totals(sym, open_positions, closed_trades)
        cards.append({
            "symbol": feat.get("symbol", sym),
            "price": feat.get("price"),
            "m5_trend": feat.get("m5_trend"),
            "m15_trend": feat.get("m15_trend"),
            "trend": f"{feat.get('m5_trend', '—')} / {feat.get('m15_trend', '—')}",
            "stoch_k": feat.get("stoch_k"),
            "stoch_d": feat.get("stoch_d"),
            "stoch": f"{feat.get('stoch_k', '—')} / {feat.get('stoch_d', '—')}",
            "stoch_cross": feat.get("stoch_cross"),
            "atr": feat.get("atr"),
            "atr_ratio": feat.get("atr_ratio"),
            "support": feat.get("support"),
            "resistance": feat.get("resistance"),
            "volatility_regime": feat.get("volatility_regime"),
            "breakout": feat.get("breakout"),
            "rejection": feat.get("rejection"),
            "volume_ratio": feat.get("volume_ratio"),
            "bb_position": feat.get("bb_position"),
            "market_regime": regime.get("primary") or ctx.get("regime"),
            "regime_bias": regime.get("bias"),
            "regime_description": regime.get("description"),
            "session": ctx.get("session"),
            "market_intent": ctx.get("market_intent"),
            "has_position": pnl["open_position_count"] > 0,
            "position_side": pnl["position_summary"],
            "position_pnl": pnl["unrealized_pnl"],
            "total_pnl": pnl["total_pnl"],
            "realized_pnl": pnl["realized_pnl"],
            "unrealized_pnl": pnl["unrealized_pnl"],
            "open_position_count": pnl["open_position_count"],
            "closed_trade_count": pnl["closed_trade_count"],
            "closed_wins": pnl["closed_wins"],
            "closed_losses": pnl["closed_losses"],
            "has_trading_activity": pnl["has_trading_activity"],
        })
    return cards


def _build_live_portfolio(
    account: dict,
    paper_orders: dict,
    paper_positions: dict,
    paper_trades: dict,
    features: dict,
) -> dict:
    """Live balance, equity, and open-trade PnL — prefer account.json over stale orders."""
    order_acct = (paper_orders or {}).get("account", {})
    balance = (paper_orders or {}).get("balance", {})
    baseline = read_json_state("mt5_baseline.json", default={})

    cash = float(
        account.get("balance")
        or order_acct.get("balance")
        or balance.get("cash")
        or 0
    )
    equity = float(
        account.get("equity")
        or order_acct.get("equity")
        or balance.get("equity")
        or cash
    )
    starting = float(
        balance.get("starting_cash")
        or baseline.get("starting_cash")
        or cash
    )

    positions = (paper_positions or {}).get("positions", [])
    unrealized_pnl = round(sum(float(p.get("profit", 0)) for p in positions), 2)

    trades = (paper_trades or {}).get("trades", [])
    realized_pnl = round(sum(float(t.get("pnl", 0)) for t in trades), 2)

    open_positions = []
    feat_symbols = (features or {}).get("symbols", {})
    for pos in positions:
        sym = pos.get("symbol")
        live_price = feat_symbols.get(sym, {}).get("price")
        open_positions.append({
            "symbol": sym,
            "side": pos.get("side"),
            "entry": pos.get("entry"),
            "size": pos.get("size"),
            "profit": float(pos.get("profit", 0)),
            "sl": pos.get("sl"),
            "tp1": pos.get("tp1"),
            "setup_type": pos.get("setup_type"),
            "live_price": live_price,
            "ticket": pos.get("ticket") or pos.get("position_id"),
        })

    session_pnl = round(equity - starting, 2)
    session_pnl_pct = round((session_pnl / starting * 100) if starting else 0, 2)

    return {
        "cash": round(cash, 2),
        "equity": round(equity, 2),
        "starting_equity": round(starting, 2),
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": realized_pnl,
        "session_pnl": session_pnl,
        "session_pnl_pct": session_pnl_pct,
        "open_positions": open_positions,
        "position_count": len(positions),
        "trade_count": len(trades),
        "account_login": account.get("login"),
        "account_server": account.get("server"),
        "source": "account.json" if account.get("equity") else "paper_orders",
        "updated_at": account.get("timestamp") or paper_orders.get("timestamp"),
    }


def _build_watchlist(candidates: list, features: dict, market_ctx: dict) -> list[dict]:
    ctx_symbols = market_ctx.get("market_context", market_ctx).get("symbols", {})
    feat_symbols = features.get("symbols", {})
    seen = {c["symbol"] for c in candidates}
    rows = []
    for c in candidates:
        sym = c["symbol"]
        regime = c.get("market_context", {}).get("market_regime", {})
        feat = feat_symbols.get(sym, {})
        rows.append({
            "symbol": sym,
            "action": c.get("side"),
            "confidence": c.get("confidence"),
            "setup_type": c.get("setup_type"),
            "regime": regime.get("primary") or c.get("market_context", {}).get("regime"),
            "price": feat.get("price"),
            "trend": feat.get("m5_trend"),
            "status": "SIGNAL",
        })
    for sym, feat in feat_symbols.items():
        if sym in seen:
            continue
        ctx = ctx_symbols.get(sym, {})
        regime = ctx.get("market_regime", {})
        rows.append({
            "symbol": sym,
            "action": "WAIT",
            "confidence": None,
            "setup_type": None,
            "regime": regime.get("primary") or ctx.get("regime"),
            "price": feat.get("price"),
            "trend": feat.get("m5_trend"),
            "status": "WAIT",
        })
    return rows


def _build_edge_insights(edge_db: dict) -> dict:
    aggregates = edge_db.get("aggregates", {})
    by_setup = aggregates.get("by_setup", {})
    setups = sorted(
        by_setup.items(),
        key=lambda x: (x[1].get("win_rate_pct", 0), x[1].get("total", 0)),
        reverse=True,
    )
    patterns = []
    for ctx_key, cell in list(aggregates.get("by_context", {}).items())[:20]:
        parts = ctx_key.split("|")
        if len(parts) < 3:
            continue
        symbol, regime, session = parts[0], parts[1], parts[2]
        for setup, stats in cell.items():
            if stats.get("total", 0) < 3:
                continue
            patterns.append({
                "symbol": symbol,
                "regime": regime,
                "session": session,
                "setup": setup,
                "win_rate_pct": stats.get("win_rate_pct", 0),
                "total": stats.get("total", 0),
            })
    patterns.sort(key=lambda p: abs(p.get("win_rate_pct", 50) - 50), reverse=True)
    return {
        "record_count": edge_db.get("count", len(edge_db.get("records", []))),
        "setups": [{"name": k, **v} for k, v in setups],
        "context_patterns": patterns[:15],
    }


def _strategy_comparison(edge_insights: dict, rankings: dict) -> list[dict]:
    rows = []
    for item in edge_insights.get("setups", []):
        rows.append({
            "setup": item["name"],
            "win_rate_pct": item.get("win_rate_pct", 0),
            "total": item.get("total", 0),
            "source": "edge_db",
        })
    if not rows:
        for sym, list_rank in rankings.get("rankings", {}).items():
            for r in list_rank:
                rows.append({
                    "setup": r.get("setup_type"),
                    "win_rate_pct": r.get("win_rate_pct", r.get("score", 0)),
                    "total": r.get("total", 0),
                    "symbol": sym,
                    "source": "ranker",
                })
    seen = set()
    unique = []
    for r in sorted(rows, key=lambda x: x.get("win_rate_pct", 0), reverse=True):
        key = r.get("setup")
        if key and key not in seen:
            seen.add(key)
            unique.append(r)
    return unique[:12]


def _build_learning(edge_scores: dict, memory: dict) -> dict:
    global_stats = edge_scores.get("setup_stats", {}).get("global", {})
    ranked = sorted(
        global_stats.items(),
        key=lambda x: (x[1].get("win_rate_pct", 0), x[1].get("total", 0)),
        reverse=True,
    ) if global_stats else []
    best = ranked[0] if ranked else None
    worst = ranked[-1] if ranked else None
    adjustments = memory.get("adjustments", [])[-8:]
    avg_conf = 0
    records = memory.get("records", [])
    conf_vals = [r.get("confidence") for r in records if r.get("confidence")]
    if conf_vals:
        avg_conf = round(sum(conf_vals) / len(conf_vals), 1)
    weight_deltas = []
    for adj in adjustments:
        delta = adj.get("delta", 0)
        if delta:
            weight_deltas.append({
                "label": adj.get("setup_type", "setup"),
                "delta": f"{delta:+.1f}%",
                "note": adj.get("adjustment", ""),
            })
    return {
        "best_setup": {"name": best[0], **best[1]} if best else None,
        "worst_setup": {"name": worst[0], **worst[1]} if worst else None,
        "setups": [{"name": k, **v} for k, v in ranked[:10]],
        "adjustments": adjustments,
        "weight_deltas": weight_deltas,
        "avg_confidence": avg_conf,
        "total_records": memory.get("total_records", len(records)),
    }


def _build_ai_decision(signal: dict | None, explain: dict | None, edge_insights: dict) -> dict | None:
    if not signal and not explain:
        return None
    src = explain or {}
    sig = signal or {}
    side = src.get("side") or sig.get("side")
    symbol = src.get("symbol") or sig.get("symbol")
    confidence = src.get("confidence") or sig.get("confidence")
    setup = (src.get("setup_type") or sig.get("setup_type") or "").replace("_", " ")
    regime = src.get("market_regime", {})
    levels = src.get("levels", {})
    entry = levels.get("entry") or sig.get("entry")
    tp1 = levels.get("tp1") or sig.get("tp1")
    expected_move = None
    if entry and tp1:
        expected_move = round(abs(float(tp1) - float(entry)), 2)

    setup_name = (src.get("setup_type") or sig.get("setup_type") or "").replace("_", " ")
    hist_wr = None
    for s in edge_insights.get("setups", []):
        if s.get("name", "").replace("_", " ") == setup_name or s.get("name") == src.get("setup_type"):
            hist_wr = s.get("win_rate_pct")
            break
    stats = src.get("setup_stats", {})
    if hist_wr is None and stats:
        hist_wr = stats.get("win_rate_pct")

    reasons = src.get("reasons") or sig.get("reasons") or [sig.get("reason", "")]
    reason_lines = [r for r in reasons if r]

    return {
        "side": side,
        "symbol": symbol,
        "confidence": confidence,
        "setup": setup,
        "regime": (regime.get("primary") or "").replace("_", " "),
        "regime_description": regime.get("description", ""),
        "trend": regime.get("bias") or regime.get("primary", ""),
        "reason_lines": reason_lines,
        "expected_move": expected_move,
        "historical_success_pct": hist_wr,
        "engines": src.get("engines", []),
        "evidence": src.get("evidence", []),
        "levels": levels,
    }


def _build_research_status(report: dict, validation: dict, candidates: dict, adaptive: dict) -> dict:
    return {
        "edge_records": report.get("edge_records", 0),
        "status": report.get("status", "unknown"),
        "patterns": report.get("patterns", [])[:10],
        "patterns_found": len(report.get("patterns", [])),
        "validation": validation,
        "weight_proposal": candidates.get("proposal"),
        "weight_message": candidates.get("message"),
        "weights_deployed": bool(adaptive.get("deployed")),
        "active_weights": adaptive.get("weights"),
        "weight_deltas": candidates.get("deltas_pct", {}),
    }


def _tail_log(lines: int = 40) -> list[str]:
    if not LOG_PATH.exists():
        return []
    try:
        content = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return content[-lines:]
    except OSError:
        return []


def _build_verdict(markdown: str) -> dict:
    """Summarize VERDICT.md for the dashboard state payload (headline + sections).

    Keeps the audited negative result visible on the dashboard so it cannot be
    silently dropped from the UI. The full markdown is served via /api/verdict.
    """
    if not markdown:
        return {"present": False, "headline": None, "sections": [], "markdown": ""}
    lines = markdown.splitlines()
    headline = next((ln.lstrip("# ").strip() for ln in lines if ln.startswith("# ")), None)
    sections = [ln.lstrip("# ").strip() for ln in lines if ln.startswith("## ")]
    return {
        "present": True,
        "headline": headline,
        "sections": sections,
        "char_count": len(markdown),
    }


def _run_replay_job(symbol: str | None, max_bars: int) -> None:
    try:
        write_json_state("replay_job.json", {
            "status": "running",
            "started_at": utc_now_iso(),
            "symbol": symbol,
            "max_bars": max_bars,
        })
        config = load_config()
        from core.replay_engine import ReplayEngine
        from core.utils import setup_logger
        logger = setup_logger("replay_job", "replay_job.log")
        engine = ReplayEngine(config, logger)
        result = engine.run(symbol=symbol, max_bars=max_bars)
        write_json_state("replay_job.json", {
            "status": "complete",
            "finished_at": utc_now_iso(),
            "symbol": symbol,
            "max_bars": max_bars,
            "result": {
                "trades_closed": result.get("trades_closed"),
                "pnl_total": result.get("pnl_total"),
                "win_rate_pct": result.get("win_rate_pct"),
                "bars_replayed": result.get("bars_replayed"),
            },
        })
    except Exception as exc:
        write_json_state("replay_job.json", {
            "status": "error",
            "finished_at": utc_now_iso(),
            "error": str(exc),
        })


def _build_trading_status(
    kill_switch: dict,
    risk_state: dict,
    candidates_data: dict,
    approved_data: dict,
    rejected_data: dict,
    *,
    live_trading_enabled: bool = False,
) -> dict:
    """Why trades are or aren't opening — surfaced on dashboard.

    `live_trading_enabled` (config execution flag) is the hard guard against real-
    money orders. When False, can_execute is forced False and a blocker is added,
    even if kill_switch is off and approved signals exist. This keeps the UI honest
    about the actual deploy-disable contract (kill_switch alone does not gate it).
    """
    kill_on = bool(kill_switch.get("kill_switch") or risk_state.get("kill_switch"))
    candidates = candidates_data.get("candidates", [])
    approved = approved_data.get("approved", [])
    rejected = rejected_data.get("rejected", [])

    blockers: list[str] = []
    daily = risk_state.get("daily_growth") or {}
    if daily.get("enabled"):
        pnl_pct = daily.get("daily_pnl_pct", 0)
        target = daily.get("target_pct", 20)
        blockers.insert(
            0,
            f"Daily growth: {pnl_pct:+.2f}% / +{target:.0f}% target "
            f"(${daily.get('daily_pnl', 0):+.2f} today)",
        )
    campaign = risk_state.get("growth_campaign") or {}
    if campaign.get("status") == "running":
        blockers.insert(
            0,
            f"30-day run: Day {campaign.get('days_elapsed')}/{campaign.get('duration_days')} "
            f"({campaign.get('campaign_pnl_pct', 0):+.2f}% since ${campaign.get('start_equity')})",
        )
        if daily.get("trading_paused") and daily.get("pause_reason"):
            blockers.insert(0, daily["pause_reason"])
    if kill_on:
        blockers.append(f"Kill switch ON: {kill_switch.get('reason') or 'risk limit'}")

    if not candidates:
        blockers.append("No candidate signals — market gates not met this cycle")
    elif not approved:
        reasons: list[str] = []
        for row in rejected:
            reason = row.get("rejection_reason") or ", ".join(row.get("failures", []))
            if reason:
                sym = row.get("symbol", "?")
                reasons.append(f"{sym}: {reason}")
        if reasons:
            blockers.append("All candidates rejected — " + "; ".join(reasons[:3]))
        else:
            blockers.append("Candidates generated but none approved")

    can_execute = not kill_on and len(approved) > 0 and live_trading_enabled
    if not live_trading_enabled:
        blockers.append("Live trading DISABLED (live_trading_enabled: false) — paper/research mode only")
    status = (
        "blocked" if kill_on or not live_trading_enabled
        else ("ready" if approved else ("scanning" if not candidates else "filtered"))
    )

    return {
        "status": status,
        "can_execute": can_execute,
        "kill_switch": kill_on,
        "kill_reason": kill_switch.get("reason"),
        "live_trading_enabled": live_trading_enabled,
        "candidate_count": len(candidates),
        "approved_count": len(approved),
        "rejected_count": len(rejected),
        "blockers": blockers,
        "last_rejections": [
            {
                "symbol": r.get("symbol"),
                "side": r.get("side"),
                "reason": r.get("rejection_reason") or ", ".join(r.get("failures", [])),
            }
            for r in rejected[:5]
        ],
    }


def aggregate_state(*, lite: bool = False) -> dict:
    config = load_config()
    runtime_mode = runtime_mode_summary(config)
    payload: dict = {
        "config": {
            "mode": config["execution"].get("mode"),
            "account_mode": config.get("mt5", {}).get("account_mode", "demo"),
            "runtime_mode": runtime_mode["label"],
            "performance_plan_active": runtime_mode["performance_plan_active"],
            "runtime_detail": runtime_mode["detail"],
            "symbols": config["mt5"]["symbols"],
            "risk": config.get("risk", {}),
            "version": config.get("app", {}).get("version", "1.0"),
            "os_name": config.get("app", {}).get("display_name", "MT5 Quant OS"),
        },
        "runtime_mode": runtime_mode,
        "remote_access": remote_access_info(
            int(config.get("app", {}).get("dashboard", {}).get("port", 8080))
        ),
    }
    for name in STATE_FILES:
        if lite and name in LITE_SKIP_STATE:
            continue
        raw = read_json_state(name, default={})
        payload[name.replace(".json", "")] = _slim_state_file(name, raw) if lite else raw

    candidates = payload.get("candidate_signals", {}).get("candidates", [])
    top_signal = candidates[0] if candidates else None
    top_explain = (
        payload.get("candidate_signals", {}).get("top_explain")
        or (top_signal.get("explain") if top_signal else None)
    )

    features_data = payload.get("features", {})
    market_ctx_data = payload.get("market_context", {})
    positions_data = payload.get("paper_positions", {})

    payload["symbols"] = features_data.get("symbols", {})
    payload["symbol_cards"] = _build_symbol_cards(
        features_data,
        market_ctx_data,
        positions_data,
        payload.get("paper_trades", {}),
    )
    payload["live_portfolio"] = _build_live_portfolio(
        payload.get("account", {}),
        payload.get("paper_orders", {}),
        positions_data,
        payload.get("paper_trades", {}),
        features_data,
    )
    payload["watchlist"] = _build_watchlist(candidates, features_data, market_ctx_data)
    if not payload["watchlist"] and payload["symbol_cards"]:
        payload["watchlist"] = [
            {
                "symbol": c["symbol"],
                "action": c.get("position_side") or "WAIT",
                "confidence": None,
                "setup_type": None,
                "regime": c.get("market_regime"),
                "price": c.get("price"),
                "trend": c.get("m5_trend"),
                "status": "POSITION" if c.get("has_position") else "WATCH",
            }
            for c in payload["symbol_cards"]
        ]
    payload["top_explain"] = top_explain
    payload["top_signal"] = top_signal
    payload["edge_insights"] = _build_edge_insights(payload.get("edge_database", {}))
    payload["learning"] = _build_learning(payload.get("edge_scores", {}), payload.get("memory", {}))
    payload["research"] = _build_research_status(
        payload.get("research_report", {}),
        payload.get("research_validation", {}),
        payload.get("weight_candidates", {}),
        payload.get("adaptive_weights", {}),
    )
    payload["strategy_comparison"] = _strategy_comparison(
        payload["edge_insights"],
        payload.get("strategy_rankings", {}),
    )
    payload["ai_decision"] = _build_ai_decision(top_signal, top_explain, payload["edge_insights"])
    payload["equity_curve"] = build_equity_curve(
        payload.get("paper_orders", {}) if lite else payload.get("paper_orders"),
        payload.get("paper_trades"),
        payload.get("account"),
        payload.get("risk_state"),
        lite=lite,
    )
    payload["trading_status"] = _build_trading_status(
        payload.get("kill_switch", {}),
        payload.get("risk_state", {}),
        payload.get("candidate_signals", {}),
        payload.get("approved_signals", {}),
        payload.get("rejected_signals", {}),
        live_trading_enabled=bool(
            config.get("execution", {}).get("live_trading_enabled", False)
        ),
    )
    payload["logs"] = _tail_log(50)
    # Surface the audited verdict (VERDICT.md) so the dashboard cannot hide the
    # researched negative result. `verdict_summary` = headline + section headings;
    # the full markdown is available via /api/verdict.
    verdict_path = ROOT / "VERDICT.md"
    verdict_markdown = verdict_path.read_text(encoding="utf-8") if verdict_path.exists() else ""
    payload["verdict"] = _build_verdict(verdict_markdown)
    dash_cfg = config.get("app", {}).get("dashboard", {})
    payload["meta"] = {
        "refresh_seconds": int(dash_cfg.get("refresh_seconds", 2)),
        "replay_default_bars": int(config.get("replay", {}).get("max_bars", 800)),
        "lite": lite,
    }
    return payload


class DashboardHandler(BaseHTTPRequestHandler):
    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self) -> None:
        body = HTML_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_html()
        elif path in ("/api/state", "/api/summary"):
            query = parse_qs(urlparse(self.path).query)
            lite = path == "/api/summary" or query.get("lite", ["0"])[0] in ("1", "true", "yes")
            self._send_json(aggregate_state(lite=lite))
        elif path.startswith("/api/state/"):
            filename = path.split("/api/state/", 1)[-1]
            if not filename.endswith(".json"):
                filename = f"{filename}.json"
            if filename in STATE_FILES:
                self._send_json(read_json_state(filename, default={}))
            else:
                self.send_error(404)
        elif path == "/api/verdict":
            verdict_path = ROOT / "VERDICT.md"
            if verdict_path.exists():
                self._send_json({
                    "present": True,
                    "markdown": verdict_path.read_text(encoding="utf-8"),
                })
            else:
                self._send_json({"present": False, "markdown": ""}, 404)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/replay":
            body = self._read_json_body()
            config = load_config()
            symbol = body.get("symbol") or config.get("replay", {}).get("symbol")
            max_bars = int(body.get("max_bars") or config.get("replay", {}).get("max_bars", 800))
            job = read_json_state("replay_job.json", default={})
            if job.get("status") == "running":
                self._send_json({"ok": False, "message": "Replay already running"}, 409)
                return
            threading.Thread(
                target=_run_replay_job,
                args=(symbol, max_bars),
                daemon=True,
            ).start()
            self._send_json({"ok": True, "message": f"Replay started: {symbol} x {max_bars} bars"})
        else:
            self.send_error(404)

    def log_message(self, format: str, *args) -> None:
        return


def run(host: str | None = None, port: int | None = None) -> None:
    # Bind 0.0.0.0 by default so the dashboard is reachable over Tailscale/LAN.
    # The prior 127.0.0.1-only default meant ONLY localhost-on-the-server could
    # load it; phones over Tailscale hit the slow in-process bot on 0.0.0.0:8080
    # instead and hung on the loading screen. Override via DASH_HOST/DASH_PORT
    # env or --host/--port CLI.
    import os as _os

    if host is None:
        host = _os.environ.get("DASH_HOST", "0.0.0.0")
    if port is None:
        port = int(_os.environ.get("DASH_PORT", "8082"))
    server = HTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    import argparse as _ap

    _p = _ap.ArgumentParser(description="MT5 Quant OS dashboard server (reads state files directly)")
    _p.add_argument("--host", default=None, help="bind host (default 0.0.0.0, or DASH_HOST)")
    _p.add_argument("--port", type=int, default=None, help="bind port (default 8082, or DASH_PORT)")
    _a = _p.parse_args()
    run(host=_a.host, port=_a.port)
