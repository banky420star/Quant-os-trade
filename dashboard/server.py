"""Desktop dashboard API for MT5 Quant OS."""

from __future__ import annotations

import ast
import json
import math
import queue
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.equity_tracker import build_equity_curve
from core.account_mode import runtime_mode_summary
from core.remote_access import remote_access_info
from core.utils import (
    _yaml_load_with_lines,
    load_config,
    read_json_state,
    utc_now_iso,
    write_json_state,
)

import logging
_LOG = logging.getLogger("dashboard.pq")

# Module-level import of the Payoff Paradox Meter (2026-07-20). Hoisted so
# SSE ticks don't re-parse the import; the inner try/except below still
# protects against a missing-module failure during install gaps (e.g. when
# core/ is partially migrated). Top-level try lets startup continue even
# if the meter surgery is mid-rollout.
try:
    _PPM = __import__(
        "core.payoff_paradox_meter",
        fromlist=["compute_payoff_paradox_meter", "write_payoff_paradox_proposal"],
    )
    _ppm_compute = _PPM.compute_payoff_paradox_meter
    _ppm_write = _PPM.write_payoff_paradox_proposal
    _LOG.debug("payoff_paradox_meter imported at module level")
except Exception as _ppm_import_err:
    _ppm_compute = None
    _ppm_write = None
    _LOG.warning("payoff_paradox_meter import failed: %s", _ppm_import_err)

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
    "forward_test_ledger.json",
    "symbol_policy_live.json",
    "evaluated_signals.json",
    "policy_scores.json",
    "best_policies.json",
    "fast_signal_cache.json",
    "fast_mode_decisions.json",
    "fast_mode_guard.json",
    "fast_mode_runtime.json",
    "learning_state.json",
    "learning_config_overrides.json",
    "thesis_review.json",
    "trade_log.json",
    "blue_guardian.json",
    "blue_guardian_actions.json",
    # Surface per-position management state + trade_manager snapshots so the
    # Profit Quality panel can recover exit-mode flags (BE/partial/stale) that
    # the TradeTracker join missed at write-back time.
    "position_management.json",
    "trade_manager.json",
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
    "trade_log.json",
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


# ----- Exit-reason → flag mapping -----
# PRIMARY source for Profit Quality BE/Partial/Stale/Trailing splits.
# Each key is the exact exit_reason string produced by trade_tracker.py
# from MT5 DEAL_REASON codes (or paper mgmt_row flags).
# None = unresolved (falls through to "unknown" bucket in segmentation).
_EXIT_REASON_FLAGS: dict[str, dict[str, bool | None]] = {
    # Full SL hits — BE never triggered. Partial TP is an INTERMEDIATE action
    # that exit_reason doesn't capture (partial may have been taken before SL).
    # partial=None so mgmt/archive can supplement when available.
    "stop_loss":            {"be": False, "partial": None,  "trailing": False, "stale": False},
    "stop_out":             {"be": False, "partial": None,  "trailing": False, "stale": False},
    "sl_hit":               {"be": False, "partial": None,  "trailing": False, "stale": False},
    # TP hits — BE was hit (trade in profit), partial TP was done
    "take_profit":          {"be": True,  "partial": True,  "trailing": False, "stale": False},
    "partial_take_profit":  {"be": True,  "partial": True,  "trailing": False, "stale": False},
    "tp1_hit":              {"be": True,  "partial": True,  "trailing": False, "stale": False},
    "tp2_hit":              {"be": True,  "partial": True,  "trailing": False, "stale": False},
    # BE explicitly triggered and closed the remaining position.
    # Partial TP is intermediate (may have been taken before BE triggered).
    "break_even_stop":      {"be": True,  "partial": None,  "trailing": False, "stale": False},
    "break_even":           {"be": True,  "partial": None,  "trailing": False, "stale": False},
    # Trailing stop — BE was hit (trail only activates after BE), partial TP ambiguous
    "trailing_stop":        {"be": True,  "partial": None,  "trailing": True,  "stale": False},
    # Time-based / stale closes — definitely stale, BE/partial unknown without mgmt_row
    "time_stop":            {"be": None,  "partial": None,  "trailing": None,  "stale": True},
    "stale":                {"be": None,  "partial": None,  "trailing": None,  "stale": True},
}


def _build_profit_quality(
    trade_log: dict,
    pos_mgmt: dict,
    trade_mgr: dict,
) -> dict:
    """Bucketize closed trades by exit-mode so the dashboard can highlight the
    worst-bleeding exit pattern in real time.

    Five segmentation axes:
      * R-multiple (8 bins + unknown)
      * Payoff absolute-USD (12 bins + unknown)
      * BE-triggered (3-way: true / false / unknown)
      * Partial-TP done (3-way)
      * Stale-close (2-way)

    Plus three 2-way crosses for the specific bleed patterns the user is hunting:
      * BE x Partial  (catches "lock-to-BE loss-as-dust" / "BE dust winners")
      * Partial x Stale (catches "BE-then-time-stop triples")
      * BE x Stale (catches "BE'd to flat and still killed by time-stop")

    Flag resolution (exit_reason is now PRIMARY):
      1. Map exit_reason string through _EXIT_REASON_FLAGS dict (80%+ coverage now).
      2. For unknown exit_reason values (mt5_close, closed_externally), consult the
         per-trade position_mgmt dict, then live join to position_management.json,
         then archive fallback (position_mgmt_archive.jsonl).
      3. Stale_tickets from position_management.json last_run.audit (time_stop_closed)
         + trade_manager.json stale_candidates override the stale flag for any reason.
      4. Anything unresolved -> None -> "unknown" bucket so the panel is honest about
         the data layer gap rather than inventing false positives.
    """
    trades = list(trade_log if isinstance(trade_log, list) else (trade_log or {}).get("trades") or [])

    # ----- stale ticket lookup from pos_mgmt last-run audit + trade_mgr ----
    # Used as stale-flag override for trades where exit_reason doesn't carry the
    # time_stop signal (e.g. mt5_close that was actually a stale close).
    stale_tickets: set[str] = set()
    try:
        last_run = (pos_mgmt or {}).get("last_run", {}) or {}
        for a in (last_run.get("audit") or []):
            if isinstance(a, dict) and a.get("status") == "time_stop_closed":
                t = a.get("ticket")
                if t is not None:
                    stale_tickets.add(str(t))
    except (AttributeError, TypeError, KeyError):
        pass
    try:
        for entry in (trade_mgr or {}).get("stale_candidates") or []:
            if isinstance(entry, dict):
                t = entry.get("ticket") or entry.get("position_id")
                if t is not None:
                    stale_tickets.add(str(t))
    except (AttributeError, TypeError, KeyError):
        pass

    # ----- mgmt state by ticket (fallback for unknown exit_reason) -----
    mgmt_positions: dict[str, dict] = {}
    try:
        positions = (pos_mgmt or {}).get("positions", {}) or {}
        if isinstance(positions, dict):
            for ticket, row in positions.items():
                if isinstance(row, dict):
                    mgmt_positions[str(ticket)] = row
    except (AttributeError, TypeError, KeyError):
        pass



    # Archive partial_tp_done lookup: flat mapping from ANY trade identifier
    # field (mt5_position, ticket, position_id, trade_id, mt5_deal) to True.
    # Built from position_mgmt_archive.jsonl. Uses ROOT (canonical path).
    _archive_partial: dict[str, bool] = {}
    try:
        _ap = ROOT / "state" / "position_mgmt_archive.jsonl"
        if _ap.exists():
            for _line in _ap.read_text(encoding="utf-8").strip().splitlines():
                _rec = json.loads(_line)
                if _rec.get("mgmt_row", {}).get("partial_tp_done"):
                    for _id_key in ("mt5_position", "ticket", "position_id", "trade_id", "mt5_deal"):
                        _iv = _rec.get(_id_key)
                        if _iv is not None and str(_iv):
                            _archive_partial[str(_iv)] = True
    except Exception:
        _archive_partial = {}

    enriched: list[dict] = []
    n_with_r = 0
    n_exit_reason_used = 0
    n_mgmt_fallback = 0
    n_mgmt_joined = 0
    n_stale_flagged = 0
    for t in trades:
        et = dict(t)
        er = str(et.get("exit_reason") or "").lower()
        ticket = str(
            et.get("mt5_position")
            or et.get("ticket")
            or et.get("position_id")
            or et.get("trade_id")
            or et.get("mt5_deal")
            or ""
        )

        # PRIMARY: derive flags from exit_reason mapping
        flags = _EXIT_REASON_FLAGS.get(er)
        if flags is not None:
            be_val = flags["be"]
            partial_val = flags["partial"]
            trailing_val = flags["trailing"]
            is_stale = flags["stale"]
            n_exit_reason_used += 1
            et["_mgmt_source"] = "exit_reason"
        else:
            # FALLBACK: unknown exit_reason (mt5_close, closed_externally, etc.)
            # Try per-trade position_mgmt, then live join, then archive.
            mgmt = (
                et.get("position_mgmt")
                if isinstance(et.get("position_mgmt"), dict)
                else {}
            )
            if not mgmt and ticket and ticket in mgmt_positions:
                mgmt = dict(mgmt_positions[ticket])
                et["position_mgmt"] = mgmt
                n_mgmt_joined += 1

            be_val = et.get("be_triggered")
            if be_val is None and "break_even" in mgmt:
                be_val = bool(mgmt.get("break_even"))
            partial_val = None
            if "partial_tp_done" in mgmt:
                partial_val = bool(mgmt.get("partial_tp_done"))
            elif et.get("partial_tp_done") is not None:
                partial_val = bool(et.get("partial_tp_done"))
            trailing_val = bool(mgmt.get("trailing")) if "trailing" in mgmt else None
            is_stale = False
            n_mgmt_fallback += 1
            et["_mgmt_source"] = et.get("_mgmt_source") or "mgmt_state"
            # Substring inference for exit_reason variants that aren't in the
            # exact-match map (e.g. "close_partial_tp1", "breakeven_exit").
            # Only fills flags that mgmt left unresolved.
            if er:
                if partial_val is None and "partial" in er:
                    partial_val = True
                if be_val is None and ("break_even" in er or "breakeven" in er):
                    be_val = True
                if trailing_val is None and "trailing" in er:
                    trailing_val = True
                if "time_stop" in er or "stale" in er:
                    is_stale = True
            # Final fallback (2026-07-22): re-derive exit_reason for mt5_close
            # trades that have mgmt enrichment from archive. Same heuristic as
            # sync_mt5_closed_deals — profit sign + stale_closed flag. Stamps
            # et["exit_reason"] so the stoploss hit rate and from_exit_reason
            # data-quality counter reflect these trades.
            if er == "mt5_close" and isinstance(mgmt, dict):
                _derived = None
                if mgmt.get("stale_closed"):
                    _derived = "time_stop"
                elif et.get("pnl") is not None:
                    try:
                        _p = float(et["pnl"])
                        if _p < 0:
                            _derived = "stop_loss"
                        elif _p > 0:
                            _derived = "take_profit"
                    except (TypeError, ValueError):
                        pass
                if _derived:
                    er = _derived
                    et["exit_reason"] = er
                    n_exit_reason_used += 1
                    n_mgmt_fallback -= 1
                    et["_mgmt_source"] = "exit_reason"
                    # Re-derive flags from the resolved exit_reason. The
                    # pnl-sign heuristic must never override flags already
                    # known from truthful mgmt state (a green close does NOT
                    # imply BE was ever triggered) — only fill the gaps.
                    _df = _EXIT_REASON_FLAGS.get(er)
                    if _df:
                        if be_val is None:
                            be_val = _df["be"]
                        if partial_val is None:
                            partial_val = _df["partial"]
                        if trailing_val is None:
                            trailing_val = _df["trailing"]
                        if not is_stale:
                            is_stale = bool(_df["stale"])

        # SUPPLEMENT: partial TP is an INTERMEDIATE action that exit_reason may
        # not capture (e.g. stop_loss after partial was taken). When exit_reason
        # leaves partial=None, consult mgmt/archive to recover the partial flag.
        # Uses multi-key scanning (mt5_position, ticket, position_id, trade_id,
        # mt5_deal) because the archive may be keyed differently per trade.
        if partial_val is None:
            _s_mgmt = (
                et.get("position_mgmt")
                if isinstance(et.get("position_mgmt"), dict)
                else {}
            )
            if not _s_mgmt:
                for _k in ("mt5_position", "ticket", "position_id", "trade_id", "mt5_deal"):
                    _v = et.get(_k)
                    if _v is None or _v == "":
                        continue
                    if str(_v) in mgmt_positions:
                        _s_mgmt = dict(mgmt_positions[str(_v)])
                        break

            if "partial_tp_done" in _s_mgmt:
                partial_val = bool(_s_mgmt.get("partial_tp_done"))
            elif et.get("partial_tp_done") is not None:
                partial_val = bool(et.get("partial_tp_done"))

        # DEDICATED ARCHIVE FALLBACK for partial_tp_done:
        # mgmt_positions (13 entries, 0 partial, overlaps 12/13 with trades)
        # blocks the original archive loop. This fallback uses a flat
        # _archive_partial dict (trade_id→True) to bypass that blind spot.
        if partial_val is None:
            for _k in ("mt5_position", "ticket", "position_id", "trade_id", "mt5_deal"):
                _v = et.get(_k)
                if _v is None or _v == "":
                    continue
                if _archive_partial.get(str(_v)):
                    partial_val = True
                    break

        # Override stale flag with stale_tickets set (catches mt5_close that was really stale)
        if ticket and ticket in stale_tickets:
            is_stale = True
        if is_stale:
            n_stale_flagged += 1

        et["_enriched_mgmt"] = {
            "break_even": be_val,
            "partial_tp_done": partial_val,
            "trailing": trailing_val,
            "stale": bool(is_stale),
        }
        if et.get("r_multiple") is not None:
            n_with_r += 1
        enriched.append(et)

    R_ORDER = ["<-1.5R", "-1.5..-1R", "-1..-0.5R", "-0.5..0R",
               "0..0.5R", "0.5..1R", "1..2R", ">=2R", "no_r"]

    def r_bin(r) -> str:
        if r is None:
            return "no_r"
        try:
            r = float(r)
        except (TypeError, ValueError):
            return "no_r"
        if r < -1.5: return "<-1.5R"
        if r < -1.0: return "-1.5..-1R"
        if r < -0.5: return "-1..-0.5R"
        if r < 0.0:  return "-0.5..0R"
        if r < 0.5:  return "0..0.5R"
        if r < 1.0:  return "0.5..1R"
        if r < 2.0:  return "1..2R"
        return ">=2R"

    # Collapsed from 12 to 8 buckets: micro-account PnL clusters in -$5..+$5 historically
    # ($0.08 wins vs $0.73 losses from the original payoff diagnosis). Wider top bins
    # for outliers but fewer low-info bins in the middle.
    PAY_ORDER = ["<-25", "-25..-5", "-5..-1", "-1..0", "exact_be",
                 "0..+1", "+1..+5", ">=+5", "no_pnl"]

    def pay_bin(p) -> str:
        if p is None:
            return "no_pnl"
        try:
            p = float(p)
        except (TypeError, ValueError):
            return "no_pnl"
        if p < -25: return "<-25"
        if p < -5:  return "-25..-5"
        if p < -1:  return "-5..-1"
        if p < 0:   return "-1..0"
        if p == 0:  return "exact_be"
        if p < 1:   return "0..+1"
        if p < 5:   return "+1..+5"
        return ">=+5"

    def _stats(rows: list[dict]) -> dict:
        n = len(rows)
        if not n:
            return {
                "n": 0, "wins": 0, "losses": 0, "wr_pct": 0.0,
                "net_pnl": 0.0, "avg_pnl": 0.0, "avg_r": 0.0,
                "sum_r": 0.0, "median_r": 0.0,
            }
        wins = sum(1 for x in rows if (x.get("pnl") or 0) > 0)
        losses = sum(1 for x in rows if (x.get("pnl") or 0) < 0)
        net_pnl = round(sum(float(x.get("pnl") or 0) for x in rows), 2)
        avg_pnl = round(net_pnl / n, 2)
        rs = [float(x["r_multiple"]) for x in rows if x.get("r_multiple") is not None]
        avg_r = round(sum(rs) / len(rs), 3) if rs else 0.0
        sum_r = round(sum(rs), 3) if rs else 0.0
        rs_sorted = sorted(rs)
        if rs_sorted:
            mid = len(rs_sorted) // 2
            if len(rs_sorted) % 2:
                median_r = rs_sorted[mid]
            else:
                median_r = round((rs_sorted[mid - 1] + rs_sorted[mid]) / 2, 3)
        else:
            median_r = 0.0
        return {
            "n": n, "wins": wins, "losses": losses,
            "wr_pct": round(100.0 * wins / n, 1),
            "net_pnl": net_pnl, "avg_pnl": avg_pnl,
            "avg_r": avg_r, "sum_r": sum_r, "median_r": median_r,
        }

    r_buckets: dict[str, list] = {b: [] for b in R_ORDER}
    pay_buckets: dict[str, list] = {b: [] for b in PAY_ORDER}
    be_segments: dict[str, list] = {"triggered": [], "not_triggered": [], "unknown": []}
    partial_segments: dict[str, list] = {"partial": [], "no_partial": [], "unknown": []}
    stale_segments: dict[str, list] = {"stale": [], "not_stale": [], "unknown": []}
    cross_be_partial: dict = {}
    cross_partial_stale: dict = {}
    cross_be_stale: dict = {}

    for t in enriched:
        em = t["_enriched_mgmt"]
        r_buckets[r_bin(t.get("r_multiple"))].append(t)
        pay_buckets[pay_bin(t.get("pnl"))].append(t)
        be = em["break_even"]
        be_key = "triggered" if be is True else "not_triggered" if be is False else "unknown"
        be_segments[be_key].append(t)
        pt = em["partial_tp_done"]
        pt_key = "partial" if pt is True else "no_partial" if pt is False else "unknown"
        partial_segments[pt_key].append(t)
        st = em["stale"]
        st_key = "stale" if st is True else "not_stale"
        stale_segments[st_key].append(t)
        cross_be_partial[(be_key, pt_key)] = cross_be_partial.get((be_key, pt_key), []) + [t]
        cross_partial_stale[(pt_key, st_key)] = cross_partial_stale.get((pt_key, st_key), []) + [t]
        cross_be_stale[(be_key, st_key)] = cross_be_stale.get((be_key, st_key), []) + [t]

    def _cross_to_table(d: dict, a_label: str, b_label: str) -> dict:
        axis_a = sorted({k[0] for k in d.keys()})
        axis_b = sorted({k[1] for k in d.keys()})
        rows = []
        for a in axis_a:
            cells = [{"axis_b": b, **_stats(d.get((a, b), []))} for b in axis_b]
            rows.append({"axis_a": a, "cells": cells})
        return {
            "axis_a_name": a_label, "axis_b_name": b_label,
            "axis_a": axis_a, "axis_b": axis_b, "rows": rows,
        }

    def _ordered_buckets(ordered: list[str], store: dict[str, list]) -> list[dict]:
        return [{"bucket": b, **_stats(store[b])} for b in ordered]

    # ----- Bleed ranking: union of every bucket; sort by net_pnl ascending, n>=3 -----
    bleed_candidates: list[dict] = []
    for label, rows in [
        ("R bucket", _ordered_buckets(R_ORDER, r_buckets)),
        ("Payoff bucket", _ordered_buckets(PAY_ORDER, pay_buckets)),
        ("BE triggered",
         [{"bucket": k, **_stats(v)} for k, v in be_segments.items()]),
        ("Partial TP",
         [{"bucket": k, **_stats(v)} for k, v in partial_segments.items()]),
        ("Stale",
         [{"bucket": k, **_stats(v)} for k, v in stale_segments.items()]),
    ]:
        for row in rows:
            if row.get("n", 0) >= 3:
                bleed_candidates.append({
                    "axis": label,
                    "bucket": row["bucket"],
                    "n": row["n"],
                    "net_pnl": row["net_pnl"],
                    "wr_pct": row.get("wr_pct", 0.0),
                    "avg_r": row.get("avg_r", 0.0),
                })
    bleed_top = sorted(bleed_candidates, key=lambda x: x["net_pnl"])[:10]

    n_total = len(enriched)
    pct_complete = round(
        100.0 * sum(
            1 for t in enriched
            if t["_enriched_mgmt"]["break_even"] is not None
            and t["_enriched_mgmt"]["partial_tp_done"] is not None
            and t.get("r_multiple") is not None
        ) / max(n_total, 1),
        1,
    )

    # ----- Payoff Paradox Meter (2026-07-20) -----
    # Live actionable intelligence tile wired to the same /api/profit_quality/stream
    # SSE channel. ratchet-up-only proposals are written opportunistically to
    # state/learning_config_overrides.json so the auto_apply_limited gate sees
    # the suggestion via the Phase 2.4 pipeline. Defensive try/except so a
    # broken meter import doesn't crash the SSE discriminator.
    _meter: dict = {}
    _meter_floor: float = 0.4
    try:
        # Local import name aliases for clarity; bindings live at module top
        # of dashboard/server.py so SSE ticks don't re-parse the import.
        _meter_floor = _resolve_current_min_r_floor()
        _meter = _ppm_compute(enriched, current_floor=_meter_floor)
        try:
            _ppm_written = _ppm_write(
                _meter, current_floor=_meter_floor,
            )
            _meter["proposal_written"] = bool(_ppm_written)
            _meter["proposal_id"] = (
                (_ppm_written or {}).get("proposal_id") if isinstance(_ppm_written, dict) else None
            )
        except Exception as _proposal_err:
            _meter["proposal_written"] = False
            _meter["proposal_error"] = str(_proposal_err)[:120]
        _meter["current_floor_resolved"] = _meter_floor

        # Stoploss hit rate KPI — pct of trades that exited via stop_loss
        _stoploss_n = sum(
            1 for t in enriched
            if str(t.get("exit_reason") or "").lower() == "stop_loss"
        )
        _stoploss_total = max(len(enriched), 1)
        _meter["stoploss_hit_rate"] = round(100.0 * _stoploss_n / _stoploss_total, 1)
        _meter["stoploss_hit_n"] = _stoploss_n
        _meter["stoploss_hit_total"] = _stoploss_total
    except Exception as _meter_err:
        _meter = {
            "error": f"meter_unavailable: {str(_meter_err)[:120]}",
            "current_floor_resolved": 0.4,
            "suggestion_text": "Meter unavailable — see server logs.",
            "proposal_written": False,
        }

    return {
        "n_total": n_total,
        "meter": _meter,
        "data_quality": {
            "archive_partial_size": len(_archive_partial),
            "trades_from_exit_reason": n_exit_reason_used,
            "trades_from_mgmt_fallback": n_mgmt_fallback,
            "trades_with_mgmt_joined": n_mgmt_joined,
            "trades_with_r_multiple": n_with_r,
            "trades_stale_flagged": n_stale_flagged,
            "pct_complete_be_and_r": pct_complete,
            "note": (
                "BE/partial/trailing flags now derived from exit_reason (DEAL_REASON codes) "
                "as PRIMARY source. Fallback to position_mgmt dict + live state join only "
                "for unknown exit_reasons (mt5_close, closed_externally). Stale = exit_reason "
                "time_stop/stale OR stale_tickets from position_management.json.last_run "
                "(time_stop_closed) + trade_manager.json.stale_candidates."
            ),
        },
        "r_buckets": _ordered_buckets(R_ORDER, r_buckets),
        "payoff_buckets": _ordered_buckets(PAY_ORDER, pay_buckets),
        "be": [{"bucket": k, **_stats(v)} for k, v in be_segments.items()],
        "partial_tp": [{"bucket": k, **_stats(v)} for k, v in partial_segments.items()],
        "stale": [{"bucket": k, **_stats(v)} for k, v in stale_segments.items()],
        "cross_be_partial": _cross_to_table(cross_be_partial, "BE", "Partial"),
        "cross_partial_stale": _cross_to_table(cross_partial_stale, "Partial", "Stale"),
        "cross_be_stale": _cross_to_table(cross_be_stale, "BE", "Stale"),
        "bleed_top": bleed_top,
    }


def _resolve_current_min_r_floor(*, fallback: float = 0.4) -> float:
    """Read state/learning_config_overrides.json for the most-recently applied
    value of trading.exits.min_r_multiple_win and return it as the meter's
    current floor. Falls back to 0.4 when no override has been applied yet
    (the operator's Tier-1 baseline).

    Walks patches + rollbacks in chronological order so a single applied
    patch + later rollback is treated as 'back to fallback'. Idempotent.
    """
    try:
        from core.utils import STATE_DIR  # late import: keeps top-of-file clean
        fp = STATE_DIR / "learning_config_overrides.json"
        if not fp.exists():
            return float(fallback)
        import json as _json
        doc = _json.loads(fp.read_text(encoding="utf-8") or "{}")
        if not isinstance(doc, dict):
            return float(fallback)
        # Walk patches newest-first; track which path has been "rolled back"
        # since last applied so the visible state matches the order of write.
        events: list[tuple[float, str, float]] = []
        for p in doc.get("patches") or []:
            if not isinstance(p, dict):
                continue
            pp = (p.get("patch") or {})
            if not (isinstance(pp.get("path"), list) and tuple(pp["path"]) == ("trading", "exits", "min_r_multiple_win")):
                continue
            ts_raw = p.get("ts")
            ts = float(ts_raw) if ts_raw is not None else 0.0
            events.append((ts, "apply", float(pp.get("value") or fallback)))
        for r in doc.get("rollbacks") or []:
            if not isinstance(r, dict):
                continue
            rp = (r.get("rollback") or {})
            if not (isinstance(rp.get("path"), list) and tuple(rp["path"]) == ("trading", "exits", "min_r_multiple_win")):
                continue
            ts_raw = r.get("ts")
            ts = float(ts_raw) if ts_raw is not None else 0.0
            events.append((ts, "rollback", float(rp.get("to") or fallback)))
        if not events:
            return float(fallback)
        # Sort by ts ascending; the last 'apply' before any subsequent 'rollback'
        # wins. If tail event is rollback, state reverts to fallback (operator
        # chose to revert). BUG-FIX (2026-07-20): prior version sorted by
        # array index which silently rewound when patches were appended
        # out-of-order. The `e[2] if False else (e[0])` dead-code leftover
        # made the bug invisible on inspection.
        events.sort(key=lambda e: e[0])
        state = float(fallback)
        for _, kind, value in events:
            if kind == "apply":
                state = value
            elif kind == "rollback":
                state = float(fallback)
        return float(state)
    except Exception as _curr_floor_err:
            _LOG.warning(
                "payoff_paradox_meter: failed to resolve current min_r floor; "
                "falling back to %s. err=%s",
                fallback, _curr_floor_err,
            )
            return float(fallback)


def _build_bot_performance(trade_log: dict) -> dict:
    """Bot-only realised PnL (magic-filtered agent trades only), kept separate
    from whole-account equity so manual trades + deposits do not mask the bot's
    real performance. trade_log.json is already magic-filtered (agent deals only).
    """
    trades = list(trade_log if isinstance(trade_log, list) else (trade_log or {}).get("trades") or [])
    pnl = [float(t.get("pnl", 0) or 0) for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    total = round(sum(pnl), 2)
    avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
    payoff = round(abs(avg_win / avg_loss), 2) if avg_loss else 0.0
    tp_hits = sum(1 for t in trades if "tp" in str(t.get("exit_reason", "")).lower())
    be_hits = sum(1 for t in trades if "break_even" in str(t.get("exit_reason", "")).lower())
    by_symbol: dict[str, dict] = {}
    for t in trades:
        s = t.get("symbol") or "?"
        p = float(t.get("pnl", 0) or 0)
        d = by_symbol.setdefault(s, {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0})
        d["trades"] += 1; d["pnl"] = round(d["pnl"] + p, 2)
        if p > 0: d["wins"] += 1
        elif p < 0: d["losses"] += 1
    for d in by_symbol.values():
        d["win_rate_pct"] = round(100 * d["wins"] / max(d["trades"], 1), 1)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / max(len(trades), 1), 1),
        "net_pnl": total,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": payoff,
        "biggest_win": round(max(pnl), 2) if pnl else 0.0,
        "biggest_loss": round(min(pnl), 2) if pnl else 0.0,
        "tp_hits": tp_hits,
        "break_even_hits": be_hits,
        "expectancy_R": float(trade_log.get("expectancy_R") or 0.0) if isinstance(trade_log, dict) else 0.0,
        "by_symbol": by_symbol,
        "note": "Bot (magic=20250625) trades only; excludes manual trades + deposits.",
    }


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


def _policy_lookup_key(symbol: str, setup: str, session: str) -> str:
    return f"{symbol}|{setup or ''}|{session or ''}"


def _lookup_best_policy(
    best_policies: dict,
    symbol: str,
    setup: str,
    session: str,
) -> dict | None:
    """Resolve best policy for symbol/setup/session from flexible state shapes."""
    if not best_policies:
        return None
    key = _policy_lookup_key(symbol, setup, session)
    by_key = (
        best_policies.get("by_key")
        or best_policies.get("policies_by_key")
        or best_policies.get("lookup")
        or {}
    )
    if isinstance(by_key, dict) and key in by_key:
        return by_key[key]

    policies = best_policies.get("policies") or best_policies.get("best") or []
    if isinstance(policies, list):
        for row in policies:
            if row.get("symbol") != symbol:
                continue
            if setup and row.get("setup") not in (setup, row.get("setup_type")):
                continue
            if session and row.get("session") not in (session, None, ""):
                continue
            return row

    sym_block = (best_policies.get("symbols") or {}).get(symbol) or {}
    if isinstance(sym_block, dict):
        if setup and setup in sym_block:
            cell = sym_block[setup]
            return cell if isinstance(cell, dict) else None
        if session and session in sym_block:
            cell = sym_block[session]
            return cell if isinstance(cell, dict) else None
    return None


def _build_evaluation_policy_row(
    signal: dict,
    *,
    best_policy: dict | None = None,
    fallback_mode: str = "shadow",
) -> dict:
    """Normalize one evaluated/skipped signal for the dashboard panel."""
    ev = signal.get("evaluation") or {}
    ep = signal.get("execution_policy") or {}
    mgmt = signal.get("management_profile") or {}
    ctx = signal.get("market_context") or {}
    session = ctx.get("session") if isinstance(ctx, dict) else None
    recent = ev.get("recent_symbol_stats") or {}
    entry_type = ep.get("entry_type") or ev.get("entry_mode") or mgmt.get("entry_type")
    trail_on = bool(mgmt.get("trailing_enabled", True))
    trail_model = (
        f"ATR×{mgmt.get('trail_atr_mult')}"
        if trail_on and mgmt.get("trail_atr_mult") is not None
        else ("off" if not trail_on else "atr")
    )

    row = {
        "symbol": signal.get("symbol"),
        "side": signal.get("side"),
        "setup": signal.get("setup_type"),
        "session": session,
        "entry_type": entry_type,
        "limit_offset_atr": (
            ep.get("entry_offset_atr")
            if entry_type == "limit"
            else mgmt.get("limit_offset_atr")
        ),
        "sl_model": mgmt.get("sl_model"),
        "sl_atr_mult": mgmt.get("sl_atr_mult"),
        "tp_model": mgmt.get("tp_model"),
        "tp1_r": mgmt.get("tp1_r"),
        "be_trigger_r": mgmt.get("break_even_trigger_r"),
        "trail_model": trail_model,
        "trail_start_r": mgmt.get("trail_start_r"),
        "policy_score": ev.get("policy_score"),
        "reason": ev.get("reason"),
        "sample_size": int(recent.get("n") or 0),
        "mode": ev.get("mode") or fallback_mode,
        "action": ev.get("action") or ep.get("action"),
        "confidence": signal.get("confidence"),
        "confidence_adjusted": ep.get("confidence_adjusted"),
    }
    if best_policy:
        row["best_policy"] = {
            "entry_type": best_policy.get("entry_type"),
            "policy_score": best_policy.get("policy_score") or best_policy.get("score"),
            "reason": best_policy.get("reason") or best_policy.get("label"),
            "sample_size": best_policy.get("sample_size") or best_policy.get("n"),
            "mode": best_policy.get("mode"),
        }
    return row


def _build_evaluation_policy(
    evaluated_data: dict,
    policy_scores: dict,
    best_policies: dict,
    config: dict,
) -> dict:
    """Dashboard-friendly evaluation policy snapshot."""
    eval_cfg = config.get("evaluation") or {}
    fallback_mode = str(evaluated_data.get("mode") or eval_cfg.get("mode") or "shadow")
    evaluated = list(evaluated_data.get("evaluated") or [])
    skipped = list(evaluated_data.get("skipped") or [])

    rows: list[dict] = []
    for signal in evaluated + skipped:
        ctx = signal.get("market_context") or {}
        session = ctx.get("session") if isinstance(ctx, dict) else None
        setup = str(signal.get("setup_type") or "")
        symbol = str(signal.get("symbol") or "")
        best = _lookup_best_policy(best_policies, symbol, setup, str(session or ""))
        rows.append(
            _build_evaluation_policy_row(
                signal,
                best_policy=best,
                fallback_mode=fallback_mode,
            )
        )

    scores_block = {}
    if policy_scores:
        scores_block = (
            policy_scores.get("scores")
            or policy_scores.get("summary")
            or policy_scores.get("by_symbol")
            or policy_scores
        )

    return {
        "timestamp": evaluated_data.get("timestamp"),
        "mode": fallback_mode,
        "enabled": bool(eval_cfg.get("enabled", True)),
        "min_policy_score": float(eval_cfg.get("min_policy_score") or 35),
        "skip_below_score": float(eval_cfg.get("skip_below_score") or 25),
        "count": len(evaluated),
        "skipped_count": evaluated_data.get("skipped_count", len(skipped)),
        "candidate_count": evaluated_data.get("candidate_count"),
        "rows": rows,
        "policy_scores": scores_block if isinstance(scores_block, dict) else {},
        "best_policies_present": bool(best_policies),
    }


def _build_learning_status(config: dict, state: dict, decisions: list, reviews: list) -> dict:
    """Dashboard snapshot for the Phase 2.4 normalized learning loop."""
    from core.learning_logger import read_jsonl
    proposals = read_jsonl("config_proposals", limit=10)
    rejected = [p for p in proposals if p.get("rejected")]
    active = [p for p in proposals if not p.get("rejected")]
    return {
        "enabled": bool((config.get("learning") or {}).get("enabled", False)),
        "mode": (config.get("learning") or {}).get("mode", "observe_only"),
        "auto_apply_disabled": (config.get("learning") or {}).get("mode", "observe_only") != "live_apply_limited",
        "reviewed_count": state.get("reviewed_count", 0),
        "rolling_win_rate_pct": state.get("rolling_win_rate_pct"),
        "rolling_expectancy_r": state.get("rolling_expectancy_r"),
        "avg_rating_by_symbol": state.get("avg_rating_by_symbol", {}),
        "mistake_counts": state.get("mistake_counts", {}),
        "last_decisions": decisions[-10:],
        "last_reviews": reviews[-10:],
        "active_proposals": active,
        "rejected_proposals": rejected,
        "updated_at": state.get("updated_at"),
    }


def _build_daily_pnl_today() -> dict:
    """Dashboard snapshot of today's (UTC-midnight onwards) closed-trade
    realized PnL and the daily-profit halt gate state.

    Powers /api/daily_pnl so the user can SEE when the bot has earned enough
    to halt itself for the day. Backed by core.daily_pnl — the gate in
    execution_loop._check_execution_allowed uses the same helper, so this
    payload is the canonical view of "did the halt fire, and where are we?".

    Return shape (stable; clients depend on these keys):
        today_realized_usd   — sum of closed-trade pnl from UTC midnight today
        threshold_usd        — config.risk.daily_profit_halt_usd (0 = disabled)
        remaining_usd        — max(threshold - today, 0) | 0 when disabled
        progress_pct         — min(today/threshold*100, 100); 0 when disabled
        halt_active          — TRUE when kill_switch.json is the daily halt
                               AND its day_stamp matches today
        halt_reason          — copy of kill_switch.reason while halt_active
        halt_day_stamp       — copy of kill_switch.day_stamp while halt_active
        halted_at            — copy of kill_switch.halted_at while halt_active
        day_stamp_today      — "YYYY-MM-DD" UTC (the boundary key)
        day_resets_at_iso    — next UTC midnight ISO timestamp (auto-reset hour)
        trade_count_today    — n closed trades since UTC midnight
        wins_today / losses_today
        by_symbol            — [{symbol, pnl, closed_count}, …] sorted by pnl desc
        status               — "halt_active" | "tracking" | "disabled"
        updated_at_iso       — wall-clock ISO timestamp of the snapshot
    """
    try:
        from datetime import date, datetime, timedelta, timezone as _tz
        config = load_config()
        from core.daily_pnl import (
            today_realized_pnl_breakdown,
            today_realized_pnl_usd,
            today_utc_day_stamp,
        )
        threshold_usd = float(
            (config.get("risk") or {}).get("daily_profit_halt_usd", 0) or 0
        )
        breakdown = today_realized_pnl_breakdown("paper_trades.json")
        pnl_today = today_realized_pnl_usd("paper_trades.json")
        stamp_today = today_utc_day_stamp()

        kill = read_json_state("kill_switch.json", default={}) or {}
        kill_reason = str(kill.get("reason") or "")
        kill_day = str(kill.get("day_stamp") or "")
        # Broaden day-stamp match to cover the post-midnight window (~up to
        # one cycle, ~15s on growth profile) where kill_switch.json still
        # carries yesterday's day_stamp until execution_loop's day-rollover
        # block clears it on the bot's NEXT cycle. Without this, the tile
        # would briefly flash "tracking" while execution is ACTUALLY frozen.
        try:
            stamp_yesterday = (
                date.fromisoformat(stamp_today) - timedelta(days=1)
            ).isoformat()
        except (ValueError, TypeError):
            stamp_yesterday = stamp_today
        halt_active = (
            bool(kill.get("kill_switch"))
            and "daily_profit_halt" in kill_reason
            and kill_day in (stamp_today, stamp_yesterday)
        )

        if threshold_usd <= 0:
            status = "disabled"
        elif halt_active:
            status = "halt_active"
        else:
            status = "tracking"

        remaining_usd = max(threshold_usd - pnl_today, 0.0)
        progress_pct = (
            round(min(pnl_today / threshold_usd * 100.0, 100.0), 1)
            if threshold_usd > 0 else 0.0
        )

        # Sort by_symbol detail by pnl desc so winners float to the top.
        # Uses breakdown.per_symbol_detail (added by core/daily_pnl) so we
        # walk paper_trades.json ONCE — previously the helper re-walked
        # the same JSON a second time just to count per-symbol closed
        # trades, and the two passes could disagree across a day rollover.
        _detail = breakdown.get("per_symbol_detail") or {}
        by_symbol = [
            {
                "symbol": sym,
                "pnl": round(float(detail.get("pnl", 0) or 0), 2),
                "closed_count": int(detail.get("closed_count", 0) or 0),
                "wins": int(detail.get("wins", 0) or 0),
                "losses": int(detail.get("losses", 0) or 0),
            }
            for sym, detail in sorted(
                _detail.items(),
                key=lambda kv: float(kv[1].get("pnl", 0) or 0),
                reverse=True,
            )
        ]

        # Next UTC midnight (auto-reset hour; relies on the hoisted
        # datetime import at the top of the function body).
        next_midnight = (
            datetime.now(_tz.utc).replace(
                hour=0, minute=0, second=0, microsecond=0,
            ) + timedelta(days=1)
        )

        return {
            "today_realized_usd": round(float(pnl_today), 2),
            "threshold_usd": float(threshold_usd),
            "remaining_usd": round(float(remaining_usd), 2),
            "progress_pct": progress_pct,
            "halt_active": bool(halt_active),
            "halt_reason": kill_reason if halt_active else None,
            "halt_day_stamp": kill_day if halt_active else None,
            "halted_at": kill.get("halted_at") if halt_active else None,
            "day_stamp_today": stamp_today,
            "day_resets_at_iso": next_midnight.isoformat(),
            "trade_count_today": int(breakdown.get("closed_count") or 0),
            "wins_today": int(breakdown.get("wins") or 0),
            "losses_today": int(breakdown.get("losses") or 0),
            "by_symbol": by_symbol,
            "status": status,
            "updated_at_iso": utc_now_iso(),
        }
    except Exception as _daily_pnl_err:
        _LOG.warning("_build_daily_pnl_today failed: %s", _daily_pnl_err)
        return {
            "today_realized_usd": 0.0,
            "threshold_usd": 0.0,
            "remaining_usd": 0.0,
            "progress_pct": 0.0,
            "halt_active": False,
            "halt_reason": None,
            "halt_day_stamp": None,
            "halted_at": None,
            "day_stamp_today": None,
            "day_resets_at_iso": None,
            "trade_count_today": 0,
            "wins_today": 0,
            "losses_today": 0,
            "by_symbol": [],
            "status": "error",
            "updated_at_iso": utc_now_iso(),
            "error": str(_daily_pnl_err)[:200],
        }


def _build_adaptive_exit_tile() -> dict:
    """Compute per-symbol adaptive exit SL/TP for each open position.

    Reads open positions from paper_positions.json and computes what the
    adaptive exit engine would set as SL, TP1, TP2 for each position
    based on current market data.

    Returns a list of per-position dicts with keys:
      symbol, side, entry, sl, tp1, tp2, current_price, atr, spread_pct_of_tp,
      sl_atr_mult, tp_atr_mult, trail_atr, exit_reason, profit_usd
    """
    config = load_config()
    features = read_json_state("features.json", default={"symbols": {}})
    positions_data = read_json_state("paper_positions.json", default={"positions": []})
    positions = list(positions_data.get("positions", []))
    feat_symbols = features.get("symbols", {})

    adaptive_exit_on = False
    try:
        from core.adaptive_exit import adaptive_exit_enabled, compute_adaptive_levels, get_symbol_config
        adaptive_exit_on = adaptive_exit_enabled(config)
    except Exception:
        return {"enabled": False, "symbols": [], "note": "adaptive_exit module unavailable"}

    rows = []
    for pos in positions:
        sym = pos.get("symbol", "")
        side = pos.get("side", "BUY")
        entry = float(pos.get("entry", 0) or 0)
        current_sl = float(pos.get("sl", 0) or 0)
        tp1 = float(pos.get("tp1", 0) or 0)
        feat = feat_symbols.get(sym, {})
        price = float(feat.get("price", entry))
        atr = float(feat.get("atr", price * 0.001) or price * 0.001)
        profit_usd = float(pos.get("profit", 0) or 0)
        spread_pts = int(feat.get("spread_points", 0) or 0)

        row = {
            "symbol": sym,
            "side": side,
            "entry": entry,
            "current_sl": current_sl,
            "current_tp1": tp1,
            "current_price": price,
            "atr": round(atr, 6),
            "profit_usd": round(profit_usd, 2),
            "spread_points": spread_pts,
        }

        if adaptive_exit_on and sym:
            try:
                cfg = get_symbol_config(sym, config)
                adaptive = compute_adaptive_levels(
                    symbol=sym,
                    side=side,
                    entry=entry,
                    atr=atr,
                    price=price,
                    spread_points=spread_pts,
                    config=config,
                )
                row["adaptive_sl"] = adaptive["sl"]
                row["adaptive_tp1"] = adaptive["tp1"]
                row["adaptive_tp2"] = adaptive["tp2"]
                row["sl_points"] = adaptive["sl_points"]
                row["tp_points"] = adaptive["tp_points"]
                row["spread_pct_of_tp"] = adaptive["spread_pct_of_tp"]
                row["sl_atr_mult"] = cfg.get("sl_atr", 0.8)
                row["tp_atr_mult"] = cfg.get("tp_m5_atr", 0.3)
                row["trail_atr"] = cfg.get("trail_atr", 0.25)
            except Exception as exc:
                row["adaptive_error"] = str(exc)[:80]

        rows.append(row)

    return {
        "enabled": adaptive_exit_on,
        "symbols": rows,
        "count": len(rows),
        "updated_at": utc_now_iso(),
        "features_available": bool(feat_symbols),
    }


def _build_living_params() -> dict:
    """Read both override files and return a combined view for the Living Parameters tile.

    Merges:
      1. symbol_be_trail_live.json — per-symbol BE/trail overrides (trusted/manual)
      2. learning_config_overrides.json — SL/TP patches from the learning loop

    Returns a flat dict the dashboard can render as a side-by-side table.
    """
    data: dict = {
        "symbol_be": {},
        "config_patches": [],
        "cumulative_patch_state": {},
        "updated_at": utc_now_iso(),
    }
    # --- symbol_be_trail_live.json ---
    be = read_json_state("symbol_be_trail_live.json", default={}) or {}
    syms = be.get("symbols") or {}
    if isinstance(syms, dict):
        rows = []
        for sym in sorted(syms.keys()):
            s = syms[sym]
            be_trigger = None
            be_row = s.get("break_even") or {}
            if isinstance(be_row, dict):
                be_trigger = be_row.get("trigger_atr_mult")
            rows.append({
                "symbol": sym,
                "n": s.get("n", 0),
                "trusted": bool(s.get("trusted")),
                "be_trigger_r": be_trigger,
                "reason": str(s.get("reason", "") or "")[:80],
                "source": str(s.get("source", "") or ""),
            })
        data["symbol_be"] = {
            "symbols": rows,
            "updated_at": be.get("updated_at"),
        }
    # --- learning_config_overrides.json ---
    lco = read_json_state("learning_config_overrides.json", default={"patches": []})
    if isinstance(lco, dict):
        patches = list(lco.get("patches") or [])
        # Compute cumulative state (last value per key wins)
        cumulative: dict = {}
        for p in patches:
            patch = p.get("patch") or {}
            if isinstance(patch, dict):
                for k, v in patch.items():
                    if isinstance(v, dict):
                        cumulative[k] = v.get("new", v.get("value", v))
                    else:
                        cumulative[k] = v
            ops = p.get("ops") or []
            for op in ops:
                path_key = ".".join(str(x) for x in op.get("path", []))
                cumulative[path_key] = op.get("value", op)
        data["config_patches"] = {
            "patch_count": len(patches),
            "rollback_count": len(lco.get("rollbacks") or []),
            "updated_at": lco.get("updated_at"),
        }
        data["cumulative_patch_state"] = cumulative
    return data


def _build_learning_timeline() -> dict:
    """Bucket Phase 2.4 decisions/reviews/proposals by hour over the last 24h.

    Returns per-hour counts so the dashboard can render a throughput heatmap
    that shows how many events were emitted per hour. Buckets with zero events
    are included so the heatmap has consistent 24 columns.
    """
    from core.learning_logger import read_jsonl
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    cut = now - timedelta(hours=24)

    # Map hour key "YYYY-MM-DDTHH" -> {decisions, reviews, proposals}
    hours: dict[str, dict[str, int]] = {}
    for h in range(24):
        ts = now - timedelta(hours=23 - h)
        key = ts.strftime("%Y-%m-%dT%H")
        hours[key] = {"decisions": 0, "reviews": 0, "proposals": 0}

    def _parse_ts(row: dict) -> str | None:
        ts = row.get("timestamp") or row.get("ts")
        if not ts:
            return None
        # Accept ISO-8601 or Unix-epoch-ms
        if isinstance(ts, str):
            try:
                return ts[:13]  # "2026-07-21T01"
            except (IndexError, TypeError):
                return None
        if isinstance(ts, (int, float)):
            try:
                dt = datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts, tz=timezone.utc)
                return dt.strftime("%Y-%m-%dT%H")
            except (OSError, ValueError):
                return None
        return None

    for row in read_jsonl("decisions", limit=8000):
        h = _parse_ts(row)
        if h and h in hours:
            hours[h]["decisions"] += 1

    for row in read_jsonl("reviews", limit=8000):
        h = _parse_ts(row)
        if h and h in hours:
            hours[h]["reviews"] += 1

    for row in read_jsonl("config_proposals", limit=8000):
        h = _parse_ts(row)
        if h and h in hours:
            hours[h]["proposals"] += 1

    # Return sorted array of hourly buckets
    timeline = []
    for key in sorted(hours.keys()):
        row = hours[key]
        timeline.append({
            "hour": key[11:],  # "01" from "2026-07-21T01"
            "label": key[5:16],  # "07-21T01"
            "decisions": row["decisions"],
            "reviews": row["reviews"],
            "proposals": row["proposals"],
            "total": row["decisions"] + row["reviews"] + row["proposals"],
        })

    total_events = sum(r["total"] for r in timeline)
    last_event = next(
        (r for r in reversed(timeline) if r["total"] > 0),
        None,
    )

    return {
        "timeline": timeline,
        "total_events_24h": total_events,
        "max_hourly": max(r["total"] for r in timeline) if timeline else 0,
        "last_event_at": last_event["label"] if last_event else None,
        "hour_count": len(timeline),
    }


# === BEGIN BLOCK: clean replacement for cli_overrides ===
# Section heading + module-level constants + helpers + _build_cli_overrides.
# This self-contained snippet is spliced into dashboard/server.py to replace
# the broken concatenation across multiple str_replace iterations.

# === BEGIN BLOCK: clean replacement for cli_overrides ===
# Section heading + module-level constants + helpers + _build_cli_overrides.
# This self-contained snippet is spliced into dashboard/server.py to replace
# the broken concatenation across multiple str_replace iterations.

# ---- /api/cli_overrides ----------------------------------------------------
# One-glance provenance for every config key. Walks the merge chain
# (config.yaml -> config.local.yaml -> active profile -> in-memory layers ->
# state overrides) and shows WHERE each key came from (file+line) so the
# next time a profile shadow beats a base edit, the user sees it instantly.
# Mtime-cached for 5s; cheap YAML walk triggered per poll once active profile
# or override files change.
_CLI_OVERRIDES_CACHE = {}
_CLI_OVERRIDES_CACHE_TTL_SEC = 5.0
_CLI_OVERRIDES_CACHE_LOCK = threading.Lock()


def _cli_overrides_mtime_iso(path):
    try:
        import os as _os
        mtime = _os.path.getmtime(path)
    except OSError:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat()


def _flatten_config_keys(value, prefix=""):
    """Flatten dicts into dot-path -> Python-value. Sequences stay at the
    parent path because core.utils._deep_merge replaces lists as atomic
    values when they overlap between layers."""
    out = {}
    if isinstance(value, dict):
        for k, v in value.items():
            child = f"{prefix}.{k}" if prefix else str(k)
            out[child] = v
            if isinstance(v, dict):
                out.update(_flatten_config_keys(v, child))
    return out


def _collect_yaml_layers():
    """Walk the YAML merge chain: base -> local -> active profile. Each layer
    carries its file path, source-line line_map, and a UTC mtime stamp."""
    layers = []
    base_path = ROOT / "config.yaml"
    if base_path.is_file():
        try:
            value, lines = _yaml_load_with_lines(base_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            _LOG.warning("cli_overrides: base load failed: %s", exc)
            value, lines = {}, {}
        layers.append({
            "name": "base",
            "source_path": str(base_path),
            "value": value,
            "lines": lines,
            "in_memory": False,
            "override_time": _cli_overrides_mtime_iso(base_path),
        })
    local_path = ROOT / "config.local.yaml"
    if local_path.is_file():
        try:
            value, lines = _yaml_load_with_lines(local_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            _LOG.warning("cli_overrides: local load failed: %s", exc)
            value, lines = {}, {}
        layers.append({
            "name": "local",
            "source_path": str(local_path),
            "value": value,
            "lines": lines,
            "in_memory": False,
            "override_time": _cli_overrides_mtime_iso(local_path),
        })
    try:
        from core.profile_launcher import active_profile_name
        prof = active_profile_name()
        if prof:
            prof_path = ROOT / "profiles" / f"{prof}.yaml"
            if prof_path.is_file():
                try:
                    value, lines = _yaml_load_with_lines(prof_path)
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    _LOG.warning("cli_overrides: profile load failed: %s", exc)
                    value, lines = {}, {}
                layers.append({
                    "name": f"profile:{prof}",
                    "source_path": str(prof_path),
                    "value": value,
                    "lines": lines,
                    "in_memory": False,
                    "override_time": _cli_overrides_mtime_iso(prof_path),
                })
    except Exception as exc:
        _LOG.debug("cli_overrides: profile discovery skipped: %s", exc)
    return layers


def _collect_state_overrides():
    """Read the two state-level override files. They don't carry per-key line
    info (records are JSON), but they DO carry override_time and a name so
    the UI can call them out when they touch config keys."""
    out = []
    try:
        lco = read_json_state("learning_config_overrides.json", default=None)
        if isinstance(lco, dict):
            patches = lco.get("patches") or []
            rollbacks = lco.get("rollbacks") or []
            ops_total = 0
            try:
                ops_total = sum(len(p.get("ops") or []) for p in patches if isinstance(p, dict))
            except (TypeError, AttributeError):
                ops_total = 0
            if patches or rollbacks or ops_total or lco.get("updated_at"):
                patch_paths = []
                for p in patches:
                    if not isinstance(p, dict):
                        continue
                    pp = p.get("patch") or {}
                    if isinstance(pp.get("path"), list):
                        patch_paths.append(".".join(str(x) for x in pp["path"]))
                out.append({
                    "name": "state_overrides:learning_config_overrides",
                    "source_path": str(STATE_DIR / "learning_config_overrides.json"),
                    "value": {
                        "_patches_count": len(patches),
                        "_rollbacks_count": len(rollbacks),
                        "_ops_count": ops_total,
                        "_patch_paths": patch_paths[-30:],
                    },
                    "lines": {},
                    "in_memory": True,
                    "override_time": lco.get("updated_at"),
                })
    except Exception as exc:
        _LOG.debug("cli_overrides: learning_config_overrides read failed: %s", exc)
    try:
        be = read_json_state("symbol_be_trail_live.json", default=None)
        if isinstance(be, dict):
            syms = be.get("symbols") or {}
            if isinstance(syms, dict) and syms:
                out.append({
                    "name": "state_overrides:symbol_be_trail_live",
                    "source_path": str(STATE_DIR / "symbol_be_trail_live.json"),
                    "value": {"_symbols_count": len(syms)},
                    "lines": {},
                    "in_memory": True,
                    "override_time": be.get("updated_at"),
                })
    except Exception as exc:
        _LOG.debug("cli_overrides: symbol_be_trail_live read failed: %s", exc)
    return out


# In-memory layer catalog. Each entry has a predicate that decides whether
# the layer ACTUALLY fired in this session (vs being declared-but-skipped).
# safety_relevant=True flips the exception fallback default to False so a
# thrown predicate never lets us falsely claim a safety layer ran.
_IN_MEMORY_LAYER_CATALOG_TEMPLATE = [
    {"name": "in_memory:blue_guardian",
     "source_path": "core/blue_guardian.py:prepare_blue_guardian_profile",
     "note": "blue_guardian profile synthesis",
     "safety_relevant": True,
     "enabled_predicate": lambda c: bool((c.get("blue_guardian") or {}).get("enabled", False))},
    {"name": "in_memory:performance_gates",
     "source_path": "core/performance_benchmark.py:sync_performance_gates",
     "note": "performance.apply_when gate",
     "safety_relevant": False,
     "enabled_predicate": lambda c: ((c.get("performance") or {}).get("apply_when") or "") not in ("", "never", "false")},
    {"name": "in_memory:practice_gates_pre",
     "source_path": "core/practice_session.py:sync_practice_gates",
     "note": "first practice-gate sync (before micro_profile)",
     "safety_relevant": False,
     "enabled_predicate": lambda c: True},
    {"name": "in_memory:arena_symbols",
     "source_path": "core/strategy_arena.py:sync_arena_symbols",
     "note": "strategy_arena active symbols",
     "safety_relevant": False,
     "enabled_predicate": lambda c: True},
    {"name": "in_memory:micro_profile",
     "source_path": "core/micro_profile.py:sync_micro_profile",
     "note": "micro account profile synthesis",
     "safety_relevant": False,
     "enabled_predicate": lambda c: True},
    {"name": "in_memory:practice_gates_post",
     "source_path": "core/practice_session.py:sync_practice_gates",
     "note": "second practice-gate sync (after micro_profile)",
     "safety_relevant": False,
     "enabled_predicate": lambda c: True},
    {"name": "in_memory:config_overrides",
     "source_path": "core/blue_guardian.py:apply_config_overrides",
     "note": "blue_guardian apply_config_overrides",
     "safety_relevant": True,
     "enabled_predicate": lambda c: True},
    {"name": "in_memory:learning_overrides",
     "source_path": "core/learning_overrides.py:apply_learning_overrides",
     "note": "Phase 2.4 live_apply_limited patches",
     "safety_relevant": True,
     "enabled_predicate": lambda c: (c.get("learning") or {}).get("mode") == "live_apply_limited"},
]


# _STATIC_FALLBACK_TEMPLATE: last-resort in-memory layer catalog used ONLY
# when AST discovery of core/utils.py:load_config returns 0 entries AND
# ast.parse threw (e.g. core/utils.py corrupted on disk). The dashboard's
# auto-discoverer is the canonical path; this constant exists so a single
# file corruption doesn't break the entire /api/cli_overrides endpoint.
# Schema matches the discovered-catalog shape so the consumer doesn't know
# which path supplied the rows.
_STATIC_FALLBACK_TEMPLATE = _IN_MEMORY_LAYER_CATALOG_TEMPLATE


def _compute_in_memory_layers(config):
    """Auto-discover in-memory layers from core/utils.py:load_config via AST.

    Replaces the static 8-entry _IN_MEMORY_LAYER_CATALOG_TEMPLATE with a
    runtime-discovered catalog. Any future engineer adding a sync_xyz(config)
    call inside load_config's ``if isinstance(config, dict):`` block is
    auto-surfaced here without any dashboard/server.py edit. Predicates
    come from each call's surrounding ``if cond:`` guard, compiled to a
    runtime-evaluable lambda with strict no-builtins eval (no filesystem
    reach). safety_relevant is derived from func-name keywords
    (guardian / config_overrides / learning_overrides / kill_switch /
    emergency_exit) but can be overridden per-name via
    state/in_memory_layer_safety.json.

    Names are mapped from the discovered func names back to the legacy
    schema (strip ``prepare_``/``sync_``/``apply_``/``activate_`` prefixes;
    special-case ``blue_guardian_profile`` -> ``blue_guardian``; drop the
    ``@<lineno>`` suffix used to disambiguate non-practice_gates calls so
    the smoke-test contract stays stable). When AST discovery fails (parse
    error, 0 entries), falls back to _STATIC_FALLBACK_TEMPLATE so the UI
    still shows 8 layers instead of zero.
    """
    cfg = config if isinstance(config, dict) else {}
    discovered: list[dict] = []
    try:
        from core.utils import (
            _inmem_compile_predicate as _compile_pred,
            discover_in_memory_layers as _discover,
            load_in_memory_safety_overrides as _load_safety,
        )
        safety_overrides = _load_safety()
        discovered = _discover(safety_overrides=safety_overrides)
    except Exception as exc:
        _LOG.warning(
            "cli_overrides: auto-discovery failed, falling back to static template: %s",
            exc,
        )

    if not discovered:
        # Catastrophic AST failure: use the legacy static template so the
        # dashboard still shows 8 layers instead of 0.
        if _IN_MEMORY_LAYER_CATALOG_TEMPLATE:
            _LOG.warning(
                "cli_overrides: AST returned 0 layers; using static template (%d entries)",
                len(_IN_MEMORY_LAYER_CATALOG_TEMPLATE),
            )
            out = []
            for e in _IN_MEMORY_LAYER_CATALOG_TEMPLATE:
                try:
                    enabled = bool(e["enabled_predicate"](cfg))
                except Exception:
                    enabled = not bool(e.get("safety_relevant", False))
                out.append({
                    "name": e["name"],
                    "source_path": e["source_path"],
                    "note": e["note"],
                    "enabled": enabled,
                    "safety_relevant": bool(e.get("safety_relevant", False)),
                })
            return out
        return []

    # Compile predicates from AST guard strings. Names referenced in the
    # guard are resolved against core/utils.py's import map (each entry
    # came from `from core.X import Y; Y(config)` style).
    imp_map: dict[str, str] = {}
    try:
        utils_text = (ROOT / "core" / "utils.py").read_text(encoding="utf-8")
        utils_tree = ast.parse(utils_text)
        for node in ast.iter_child_nodes(utils_tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imp_map[alias.asname or alias.name] = node.module
    except Exception:
        pass

    out = []
    for entry in discovered:
        predicate = None
        try:
            predicate = _compile_pred(
                entry.get("predicate_str") or "True",
                imp_map,
            )
        except Exception:
            predicate = None
        try:
            enabled = bool(predicate(cfg)) if predicate is not None else True
        except Exception:
            enabled = not bool(entry.get("safety_relevant", False))

        func_name = entry.get("func_name", "") or ""
        disambig = entry.get("disambig", "") or ""

        # Map short_name: strip common verb prefixes, special-case
        # blue_guardian_profile -> blue_guardian so the smoke-test schema
        # keeps the legacy names.
        short_name = func_name
        for prefix in ("prepare_", "sync_", "apply_", "activate_"):
            if short_name.startswith(prefix):
                short_name = short_name[len(prefix):]
                break
        if short_name == "blue_guardian_profile":
            short_name = "blue_guardian"

        # Only keep the disambig when it starts with underscore
        # (i.e. _pre / _post). Drop "@lineno" suffix used only as a
        # disambiguation hint in the discovered catalog.
        if not disambig.startswith("_"):
            disambig = ""

        catalog_name = f"in_memory:{short_name}{disambig}"
        out.append({
            "name": catalog_name,
            "source_path": entry["source_path"],
            "note": (
                f"Auto-discovered from core/utils.py:load_config \u2014 {func_name}"
            ),
            "enabled": enabled,
            "safety_relevant": bool(entry.get("safety_relevant", False)),
        })
    return out


def _build_cli_overrides(*, force=False):
    """Per-key provenance view of the entire config merge chain.

    Mtime-cached for ``_CLI_OVERRIDES_CACHE_TTL_SEC``. Use ``force=True`` from
    anything that wants to bypass the cache (e.g. an admin button).

    Returns a JSON-friendly dict with:
      - updated_at, active_profile
      - layers: ordered input chain summary (name + source_path + key_count)
      - overrides: per-key history (key, value, base_value, winning_layer,
        source_path, line_number, override_time, history[])
      - by_layer: dict[layer_name] -> list of keys that layer last won
      - hot_conflicts: keys with 3+ distinct values across YAML layers
      - summary: aggregate counts
    """
    now = time.time()
    cache_key = "_cli_overrides_v1"
    cached = _CLI_OVERRIDES_CACHE.get(cache_key)
    if not force and cached and (now - cached["_ts"]) < _CLI_OVERRIDES_CACHE_TTL_SEC:
        return cached["payload"]

    try:
        yaml_layers = _collect_yaml_layers()
        state_layers = _collect_state_overrides()
    except Exception as exc:
        _LOG.warning("cli_overrides: layer discovery failed: %s", exc)
        yaml_layers, state_layers = [], []

    try:
        merged_cfg = load_config() or {}
        if not isinstance(merged_cfg, dict):
            merged_cfg = {}
    except Exception as exc:
        _LOG.warning(
            "cli_overrides: load_config() failed: %s; in-memory predicates defaulting to enabled=True",
            exc,
        )
        merged_cfg = {}

    flat_layers = []
    base_flat = {}
    for layer in yaml_layers:
        flat = _flatten_config_keys(layer.get("value") or {})
        flat_layers.append({
            "name": layer["name"],
            "source_path": layer.get("source_path"),
            "lines": layer.get("lines") or {},
            "in_memory": False,
            "override_time": layer.get("override_time"),
            "_flat": flat,
        })
        if layer["name"] == "base":
            base_flat = flat

    all_keys = set()
    for layer in flat_layers:
        all_keys.update(layer["_flat"].keys())

    in_memory_layers = _compute_in_memory_layers(merged_cfg)
    in_memory_by_name = {l["name"]: l for l in in_memory_layers}

    overrides = []
    by_layer_keys = {layer["name"]: [] for layer in flat_layers}
    for key in sorted(all_keys):
        history = []
        for layer in flat_layers:
            if key not in layer["_flat"]:
                continue
            value = layer["_flat"][key]
            history.append({
                "layer": layer["name"],
                "value": value,
                "source_path": layer.get("source_path"),
                "line_number": layer.get("lines", {}).get(key),
                "override_time": layer.get("override_time"),
                "in_memory": False,
            })
        # Surface state-overlay candidates that may STOMP this key on promote.
        for sl in state_layers:
            if sl["name"].endswith("learning_config_overrides"):
                patch_paths = (sl["value"] or {}).get("_patch_paths") or []
                if key in patch_paths:
                    history.append({
                        "layer": sl["name"],
                        "value": None,
                        "source_path": sl["source_path"],
                        "line_number": None,
                        "override_time": sl["override_time"],
                        "in_memory": True,
                        "state_overlay_only": True,
                    })
            if sl["name"].endswith("symbol_be_trail_live"):
                if (
                    key.startswith("trading.break_even.per_symbol.")
                    or key.startswith("trading.trailing.per_symbol.")
                ):
                    history.append({
                        "layer": sl["name"],
                        "value": None,
                        "source_path": sl["source_path"],
                        "line_number": None,
                        "override_time": sl["override_time"],
                        "in_memory": True,
                        "state_overlay_only": True,
                    })

        winning_layer = None
        winning_value = None
        winning_path = None
        winning_line = None
        winning_time = None
        for entry in reversed(history):
            if not entry.get("in_memory"):
                winning_layer = entry["layer"]
                winning_value = entry["value"]
                winning_path = entry["source_path"]
                winning_line = entry["line_number"]
                winning_time = entry["override_time"]
                break
        if winning_layer is None and history:
            tail = history[-1]
            winning_layer = tail["layer"]
            winning_path = tail["source_path"]
            winning_line = tail["line_number"]
            winning_time = tail["override_time"]

        base_value = base_flat.get(key)
        if base_value is None and key not in base_flat:
            shadowed_status = "introduced_by_overlay"
        elif winning_value is None:
            shadowed_status = "shadowed_to_none"
        else:
            shadowed_status = "shadowed" if winning_value != base_value else "in_sync"

        overrides.append({
            "key": key,
            "value": winning_value,
            "base_value": base_value,
            "shadowed": shadowed_status != "in_sync",
            "shadowed_status": shadowed_status,
            "winning_layer": winning_layer,
            "source_path": winning_path,
            "line_number": winning_line,
            "override_time": winning_time,
            "history": history,
        })
        if winning_layer:
            by_layer_keys.setdefault(winning_layer, []).append(key)

    # Hot conflicts: any key where YAML layers handed 3+ distinct values.
    hot = []
    for o in overrides:
        distinct = []
        seen = set()
        truncated = False
        for h in o["history"]:
            if h.get("in_memory"):
                continue
            try:
                tag = repr(h["value"])
            except Exception:
                tag = str(h["value"])
            if len(tag) > 200:
                tag = tag[:200] + "\u2026"
                truncated = True
            if tag not in seen:
                seen.add(tag)
                distinct.append(tag)
        if len(distinct) >= 3:
            hot.append({
                "key": o["key"],
                "distinct_values": len(distinct),
                "truncated": truncated,
                "history": o["history"],
            })

    active_profile = None
    for layer in flat_layers:
        if layer["name"].startswith("profile:"):
            active_profile = layer["name"].split(":", 1)[1]

    layers_summary = [
        {
            "name": layer["name"],
            "source_path": layer.get("source_path"),
            "in_memory": bool(layer.get("in_memory")),
            "override_time": layer.get("override_time"),
            "key_count": len(layer.get("_flat") or layer.get("value") or {}),
        }
        for layer in flat_layers
    ] + [
        {
            "name": entry["name"],
            "source_path": entry["source_path"],
            "in_memory": True,
            "override_time": None,
            "key_count": 0,
            "note": entry["note"],
            "enabled": entry.get("enabled", True),
        }
        for entry in in_memory_layers
    ] + [
        {
            "name": sl["name"],
            "source_path": sl["source_path"],
            "in_memory": True,
            "override_time": sl["override_time"],
            "key_count": (
                len((sl.get("value") or {}).get("_patch_paths") or [])
                if "_patch_paths" in (sl.get("value") or {}) else 0
            ),
        }
        for sl in state_layers
    ]

    base_size_lines = 0
    base_path = ROOT / "config.yaml"
    if base_path.is_file():
        try:
            base_size_lines = sum(
                1 for ln in base_path.read_text(encoding="utf-8").splitlines() if ln.strip()
            )
        except OSError:
            base_size_lines = 0

    payload = {
        "updated_at": utc_now_iso(),
        "active_profile": active_profile,
        "layers": layers_summary,
        "overrides": overrides,
        "by_layer": {k: v for k, v in by_layer_keys.items() if v},
        "hot_conflicts": hot,
        "summary": {
            "total_keys_tracked": len(all_keys),
            "shadowed_keys": sum(1 for o in overrides if o["shadowed"]),
            "introduced_keys": sum(
                1 for o in overrides if o["shadowed_status"] == "introduced_by_overlay"
            ),
            "hot_conflicts_count": len(hot),
            "base_config_yaml_lines": base_size_lines,
            "cache_ttl_sec": _CLI_OVERRIDES_CACHE_TTL_SEC,
        },
    }
    with _CLI_OVERRIDES_CACHE_LOCK:
        prev = _CLI_OVERRIDES_CACHE.get(cache_key)
        if prev is None or prev["_ts"] <= now or force:
            _CLI_OVERRIDES_CACHE[cache_key] = {"_ts": now, "payload": payload}
    return payload
# === END BLOCK ===

def _build_fast_mode_status(
    config: dict,
    cache: dict,
    decisions: dict,
    guard: dict,
    supervisor: dict,
    runtime: dict | None = None,
) -> dict:
    """Dashboard snapshot for the two-speed fast scalper layer."""
    from core.fast_mode import fast_mode_settings
    from core.fast_mode_runtime import preset_catalog

    fm_cfg = fast_mode_settings(config)
    yaml_cfg = config.get("fast_mode") or {}
    enabled = bool(fm_cfg.get("enabled"))
    live = bool(fm_cfg.get("live_enabled"))
    rt = runtime if runtime is not None else read_json_state("fast_mode_runtime.json", default={})
    svc = next(
        (s for s in (supervisor.get("services") or []) if s.get("name") == "fast_mode"),
        None,
    )
    cache_syms = dict(cache.get("symbols") or {})
    cache_rows = []
    for sym, row in cache_syms.items():
        mgmt = row.get("management_profile") or {}
        cache_rows.append({
            "symbol": sym,
            "side": row.get("side"),
            "setup": row.get("setup_type"),
            "anchor": row.get("anchor"),
            "entry_zone": row.get("entry_zone"),
            "entry_type": row.get("entry_type"),
            "expires_at": row.get("expires_at"),
            "be_trigger_r": mgmt.get("break_even_trigger_r"),
            "trail_start_r": mgmt.get("trail_start_r"),
        })
    dec_list = list(decisions.get("decisions") or [])
    guard_list = list(guard.get("actions") or [])
    return {
        "enabled": enabled,
        "live_enabled": live,
        "mode": "live" if live else "observe",
        "tick_interval_ms": int(fm_cfg.get("tick_interval_ms") or 1000),
        "symbols": list(fm_cfg.get("symbols") or []),
        "active_preset": rt.get("preset"),
        "preset_label": rt.get("label"),
        "runtime_overrides": rt.get("overrides") or {},
        "yaml_live_enabled": bool(yaml_cfg.get("live_enabled")),
        "presets": preset_catalog(),
        "tick_interval_note": (
            "Tick interval changes apply after bot restart"
            if rt.get("overrides", {}).get("tick_interval_ms")
            != yaml_cfg.get("tick_interval_ms")
            else ""
        ),
        "service_status": (svc or {}).get("status") or ("not_registered" if enabled else "disabled"),
        "service_last_run": (svc or {}).get("last_run"),
        "service_duration_ms": (svc or {}).get("last_duration_ms"),
        "cache_symbol_count": cache.get("symbol_count") or len(cache_syms),
        "cache_rows": cache_rows,
        "decisions": dec_list[-12:],
        "guard_actions": guard_list[-12:],
        "timestamp": decisions.get("timestamp") or cache.get("timestamp") or guard.get("timestamp"),
    }


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
    bg = read_json_state("blue_guardian.json", default={}) or {}
    if bg.get("enabled"):
        blockers.insert(
            0,
            f"Blue Guardian: day {bg.get('daily_pnl', 0):+.2f} / +${bg.get('daily_profit_target_usd', 300):.0f} "
            f"({bg.get('daily_profit_target_pct', 6):.0f}%) · floating check -$35/-$45",
        )
        if bg.get("trading_paused") and bg.get("pause_reason"):
            blockers.insert(0, bg["pause_reason"])

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
    runtime_mode = dict(runtime_mode_summary(config))
    _ap = read_json_state("active_profile.json", default={}) or {}
    if _ap.get("profile"):
        runtime_mode["active_profile"] = _ap["profile"]
    payload: dict = {
        "config": {
            "mode": config["execution"].get("mode"),
            "account_mode": config.get("mt5", {}).get("account_mode", "demo"),
            "runtime_mode": runtime_mode["label"],
            "performance_plan_active": runtime_mode["performance_plan_active"],
            "runtime_detail": runtime_mode["detail"],
            "symbols": config["mt5"]["symbols"],
            "risk": config.get("risk", {}),
            "blue_guardian": config.get("blue_guardian", {}),
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
    payload["bot_performance"] = _build_bot_performance(payload.get("trade_log", {}))
    payload["profit_quality"] = _build_profit_quality(
        payload.get("trade_log", {}),
        payload.get("position_management", {}),
        payload.get("trade_manager", {}),
    )
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
    payload["evaluation_policy"] = _build_evaluation_policy(
        payload.get("evaluated_signals", {}),
        payload.get("policy_scores", {}),
        payload.get("best_policies", {}),
        config,
    )
    payload["fast_mode"] = _build_fast_mode_status(
        config,
        payload.get("fast_signal_cache", {}),
        payload.get("fast_mode_decisions", {}),
        payload.get("fast_mode_guard", {}),
        payload.get("supervisor", {}),
        payload.get("fast_mode_runtime", {}),
    )
    from core.learning_logger import read_jsonl as _rl
    # Merge the Phase-2.4 learning-loop status INTO the learning object built
    # above (_build_learning), instead of replacing it. Replacing dropped
    # best_setup/worst_setup/setups/adjustments/avg_confidence/total_records,
    # which left the dashboard's Setup Performance / Best Setup / Worst Setup /
    # Avg Confidence / Total Records / Recent Adjustments panels empty.
    payload["learning"].update(_build_learning_status(
        config,
        payload.get("learning_state", {}) or {},
        _rl("decisions", limit=10),
        _rl("reviews", limit=10),
    ))
    payload["logs"] = _tail_log(50)
    # Surface the bot's MT5 connection state as a top-level `connection` field
    # so the SPA's "MT5 Connected/Offline" pill reflects reality. health.json
    # (written by the bot's health loop) carries the authoritative
    # {alive, logged_in, latency_ms, ping:{...}} block; without this mapping the
    # SPA reads conn.logged_in=None and always shows "MT5 Offline" even when the
    # bot is connected at ~2ms. Falls back to account.json's connected flag.
    health_conn = (payload.get("health") or {}).get("connection") or {}
    if not health_conn.get("logged_in"):
        acc = payload.get("account") or {}
        health_conn = {**health_conn, "logged_in": bool(acc.get("connected"))}
    payload["connection"] = health_conn
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


def _sanitize_json(obj):
    """Replace non-finite floats (inf, -inf, nan) with None so the response is
    spec-compliant JSON. Python's json.dumps emits bare `Infinity`/`-Infinity`/
    `NaN` tokens by default (allow_nan=True); browsers' JSON.parse REJECT those
    tokens (SyntaxError). That broke the SPA dashboard for ~hours: every
    /api/summary fetch returned 200 but res.json() threw because the culturing
    ledger cells carry profit_factor=Infinity, so safeRender never received
    data and the page stayed "unpopulated". Sanitizing at serialization fixes
    every API response in one place. Recursive over dict/list/float."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(v) for v in obj]
    return obj


def _json_default_safe(obj):
    """Fallback for json.dumps(default=...) that replaces non-finite floats
    (inf, -inf, nan) with strings so the SSE event payload stays valid JSON.
    Python's default json.dumps emits bare Infinity/-Infinity/NaN tokens which
    browsers' JSON.parse REJECT."""
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
    return str(obj)


class _ProfitQualityBroadcaster:
    """Module-level singleton that polls _build_profit_quality() every TICK_S
    and fans out new snapshots to subscriber queues when the data fingerprint
    changes. Idle ticks push a :heartbeat so the SSE keepalive cadence is
    bounded. Used by DashboardHandler._handle_sse_profit_quality → /api/profit_quality/stream."""

    TICK_S = 5.0

    _LOCK = threading.Lock()
    _SUBSCRIBERS: list[queue.Queue] = []
    _LAST_SNAPSHOT: dict | None = None
    _LAST_FINGERPRINT: tuple | None = None
    _THREAD: threading.Thread | None = None
    _STOP = threading.Event()

    @classmethod
    def _ensure_loop(cls) -> None:
        """Spawn the daemon loop once. Idempotent — second call is a no-op
        while the prior thread is alive."""
        if cls._THREAD and cls._THREAD.is_alive():
            return
        cls._STOP.clear()
        cls._THREAD = threading.Thread(
            target=cls._loop, name="profit-quality-stream",
            daemon=True,
        )
        cls._THREAD.start()

    @classmethod
    def _tick_once(cls, build_fn=None) -> tuple:
        """One broadcaster iteration. Returns ('snapshot', snap) when the
        fingerprint changes since the prior tick, ('heartbeat', {ts}) on
        idle, ('heartbeat', {ts, error}) on error. Public so tests can
        drive it deterministically without sleeping."""
        try:
            snap = (build_fn or cls._default_build)()
        except (AttributeError, TypeError, KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
            return ("heartbeat", {"ts": utc_now_iso(), "error": str(exc)})
        if not isinstance(snap, dict):
            return ("heartbeat", {"ts": utc_now_iso(), "error": "non-dict snap"})
        try:
            fp = cls._fingerprint(snap)
        except (AttributeError, TypeError, KeyError, ValueError) as exc:
            return ("heartbeat", {"ts": utc_now_iso(), "error": f"fp:{exc}"})
        if fp == cls._LAST_FINGERPRINT:
            return ("heartbeat", {"ts": utc_now_iso()})
        cls._LAST_FINGERPRINT = fp
        cls._LAST_SNAPSHOT = snap
        return ("snapshot", snap)

    @classmethod
    def _default_build(cls):
        """Default build_fn for the broadcaster: reads only the 3 files
        _build_profit_quality needs (aggregate_state() reads ~40 which is
        wasteful for a 5s hot loop) and invokes _build_profit_quality with
        POSITIONAL args matching its (trade_log, pos_mgmt, trade_mgr)
        parameter list.

        REGRESSION GUARD (2026-07-20): prior wiring passed
        `_build_profit_quality(**{"trade_log": ..., "position_management": ...,
        "trade_manager": ...})` which raised TypeError on every live tick
        because the canonical signature uses pos_mgmt/trade_mgr (not
        position_management/trade_manager). The broadcaster caught the
        TypeError gracefully and kept sending heartbeat frames with
        err=_build_profit_quality() got an unexpected keyword argument
        'position_management' — which silently broke the SPA's real-time
        feel for the entire first day of the rollout. Read the locals as
        separate names so future param renames are obvious.

        Uses `read_json_state` from dashboard.server (which the unit tests
        can monkeypatch) rather than core.utils directly — `from core.utils
        import read_json_state` binds a local module-level reference."""
        trade_log = read_json_state("trade_log.json", default={})
        position_management = read_json_state("position_management.json", default={})
        trade_manager = read_json_state("trade_manager.json", default={})
        return _build_profit_quality(trade_log, position_management, trade_manager)

    @classmethod
    def _loop(cls) -> None:
        while not cls._STOP.is_set():
            event = cls._tick_once()
            if event is not None:
                cls._fanout(event)
            cls._STOP.wait(cls.TICK_S)

    @classmethod
    def _fanout(cls, event) -> None:
        """Push event to every subscriber queue. Drop dead subscribers — a
        continuously full queue indicates a broken TCP sink; the SSE
        handler's BrokenPipeError catch removes its queue on the next
        attempt. Public so tests can drive the exact same code path that
        _loop uses (without sleeping 5s)."""
        with cls._LOCK:
            dead: list[queue.Queue] = []
            for q in cls._SUBSCRIBERS:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    cls._SUBSCRIBERS.remove(q)
                except ValueError:
                    pass

    @classmethod
    def _fingerprint(cls, snap: dict) -> tuple:
        """Cheap diff signal (~300-500 bytes vs ~50KB snapshot JSON). Captures
        bucket-level sums for every visible panel AND the cross-tables so a
        single-cell flip in (say) BE×Partial triggers a push — that's the
        bleed-diagnostic the user built these tiles to surface. Without the
        cross-table entries a cell-level change would not be reflected in
        the dashboard for up to TICK_S seconds even though SSE ostensibly
        replaced the polling path."""
        dq = snap.get("data_quality") or {}

        def _row_sig(b: dict) -> tuple:
            return (b.get("bucket"), b.get("n"), round(float(b.get("net_pnl", 0) or 0), 2))

        def _bleed_sig(b: dict) -> tuple:
            return (b.get("axis"), b.get("bucket"), round(float(b.get("net_pnl", 0) or 0), 2))

        def _cell_sig(cell: dict) -> tuple:
            return (
                cell.get("axis_b"),
                cell.get("n"),
                round(float(cell.get("net_pnl", 0) or 0), 2),
            )

        def _cross_sig(table: dict | None) -> tuple:
            """Flatten a cross-table into a sorted tuple of (axis_a, *cell) for
            each non-empty row. axis_a is included so axis-order flips show up."""
            if not isinstance(table, dict):
                return ()
            rows = table.get("rows") or []
            sig = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                axis_a = row.get("axis_a")
                for cell in row.get("cells") or []:
                    if not isinstance(cell, dict):
                        continue
                    sig.append((axis_a, *_cell_sig(cell)))
            return tuple(sorted(sig))

        m = snap.get("meter") or {}
        return (
            snap.get("n_total"),
            dq.get("pct_complete_be_and_r"),
            tuple(sorted(_row_sig(b) for b in snap.get("r_buckets", []) if isinstance(b, dict))),
            tuple(sorted(_row_sig(b) for b in snap.get("payoff_buckets", []) if isinstance(b, dict))),
            tuple(sorted(_bleed_sig(b) for b in snap.get("bleed_top", []) if isinstance(b, dict))),
            _cross_sig(snap.get("cross_be_partial")),
            _cross_sig(snap.get("cross_partial_stale")),
            _cross_sig(snap.get("cross_be_stale")),
            tuple(sorted(_row_sig(b) for b in snap.get("be", []) if isinstance(b, dict))),
            tuple(sorted(_row_sig(b) for b in snap.get("partial_tp", []) if isinstance(b, dict))),
            # Payoff Paradox Meter signal (2026-07-20): SSE pushes whenever any
            # of these four scalars flips so the tile reflects the live state
            # within one ticker instead of waiting for an n_total cell flip.
            (
                m.get("incremental_WR"),
                m.get("BE_floor_suppressed_count"),
                m.get("median_winner_r"),
                m.get("suggested_next_floor"),
                bool(m.get("proposal_written")),
                str(m.get("suggestion_text") or "")[:80],
            ),
        )

    @classmethod
    def subscribe(cls) -> queue.Queue:
        """Register a subscriber. Pure register — does NOT start the broadcaster
        thread. Thread must be running (run() in dashboard/server.py calls
        _ensure_loop() once at server boot) or the subscriber's queue will only
        fill when something else ticks the broadcaster (e.g. test fixtures
        drive _tick_once() directly).

        The cached-snapshot push happens UNDER _LOCK so a concurrent _tick_once
        can't deliver a half-built dict reference to a fresh subscriber."""
        q: queue.Queue = queue.Queue(maxsize=8)
        with cls._LOCK:
            cls._SUBSCRIBERS.append(q)
            if cls._LAST_SNAPSHOT is not None:
                try:
                    q.put_nowait(("snapshot", cls._LAST_SNAPSHOT))
                except queue.Full:
                    pass
        return q

    @classmethod
    def unsubscribe(cls, q: queue.Queue) -> None:
        with cls._LOCK:
            try:
                cls._SUBSCRIBERS.remove(q)
            except ValueError:
                pass

    @classmethod
    def reset_for_tests(cls) -> None:
        """Stop the daemon loop, clear all subscribers. Called from the test
        suite via an autouse fixture."""
        cls._STOP.set()
        if cls._THREAD:
            cls._THREAD.join(timeout=2.0)
        cls._STOP.clear()
        cls._THREAD = None
        with cls._LOCK:
            cls._SUBSCRIBERS.clear()
        cls._LAST_FINGERPRINT = None
        cls._LAST_SNAPSHOT = None


class ThreadingDashboardServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTPServer variant. daemon_threads=True so SSE handlers don't
    block server shutdown — when the parent process exits the daemon SSE
    threads die even if a long-lived connection is still open."""
    daemon_threads = True
    allow_reuse_address = True


def _handle_sse_profit_quality(handler):
    """Long-lived SSE handler for /api/profit_quality/stream. Subscribes to the
    module-level _ProfitQualityBroadcaster; fans out 'snapshot' / 'heartbeat'
    events to the client. Pipe-broken clients are detected on wfile write and
    unsubscribed in the finally block so the broadcaster's _SUBSCRIBERS list
    doesn't leak. Designed for ThreadingDashboardServer (one thread per SSE
    client) so a slow consumer can't block the rest of the dashboard.

    Routed from DashboardHandler.do_GET by prefix /api/profit_quality/stream.
    Receives the handler instance; uses handler.wfile / handler.send_response
    / handler.send_header / handler.end_headers. Catch on BrokenPipeError /
    ConnectionResetError / OSError covers the standard client-disconnect paths
    (browser back button, network drop, operator close).
    """
    # Disable Nagle so small heartbeat / snapshot chunks leave immediately.
    try:
        handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (AttributeError, OSError):
        pass

    handler.send_response(200)
    for name, value in (
        ("Content-Type", "text/event-stream"),
        ("Cache-Control", "no-cache, no-store, must-revalidate"),
        ("Connection", "keep-alive"),
        ("X-Accel-Buffering", "no"),       # disable nginx/render-proxy buffering
        ("Retry-After", "5"),              # hint SPA retry cadence if dropped
    ):
        handler.send_header(name, value)
    handler.end_headers()

    # Hint the client the connection is live BEFORE the first snapshot lands.
    try:
        handler.wfile.write(b": initializing\n\n")
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        return

    q = _ProfitQualityBroadcaster.subscribe()
    try:
        while True:
            try:
                event_type, payload = q.get(timeout=30.0)
            except queue.Empty:
                try:
                    handler.wfile.write(b": keepalive\n\n")
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
                continue
            try:
                if event_type == "snapshot":
                    # Wrap in profit_quality + meter envelope so the SPA's
                    # renderProfitQuality(data) and renderPayoffParadoxMeter(data)
                    # both read the correct keys regardless of source (SSE vs
                    # aggregate_state polling path).
                    envelope = {
                        "profit_quality": payload,
                        "meter": payload.get("meter", {}),
                    }
                    data = json.dumps(envelope, default=_json_default_safe)
                    chunk = f"event: snapshot\ndata: {data}\n\n"
                    handler.wfile.write(chunk.encode("utf-8"))
                else:  # heartbeat
                    ts = ""
                    if isinstance(payload, dict):
                        ts = str(payload.get("ts", ""))
                        if payload.get("error"):
                            ts = f"{ts} err={payload['error']}"
                    handler.wfile.write(f": heartbeat {ts}\n\n".encode("utf-8"))
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                break
    finally:
        _ProfitQualityBroadcaster.unsubscribe(q)


class DashboardHandler(BaseHTTPRequestHandler):
    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(_sanitize_json(data), default=str).encode("utf-8")
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
        elif path.startswith("/api/trade_journal/symbol/"):
            symbol = path.split("/api/trade_journal/symbol/", 1)[-1].strip("/")
            safe_sym = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in symbol)
            if not safe_sym or safe_sym in (".", ".."):
                self.send_error(404)
                return
            data = read_json_state(f"trade_journal/symbols/{safe_sym}.json", default={})
            if not data:
                self.send_error(404)
                return
            self._send_json(data)
        elif path.startswith("/api/trade_journal/"):
            rest = path.split("/api/trade_journal/", 1)[-1].strip("/")
            parts = [p for p in rest.split("/") if p]
            if len(parts) == 2:
                safe_sym = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in parts[0])
                safe_tid = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in parts[1])
                if not safe_sym or not safe_tid or safe_sym in (".", "..") or safe_tid in (".", ".."):
                    self.send_error(404)
                    return
                data = read_json_state(f"trade_journal/symbols/{safe_sym}/{safe_tid}.json", default={})
            else:
                safe_tid = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in parts[0] if parts)
                if not safe_tid or safe_tid in (".", ".."):
                    self.send_error(404)
                    return
                data = read_json_state(f"trade_journal/{safe_tid}.json", default={})
            if not data:
                self.send_error(404)
                return
            self._send_json(data)
        elif path.startswith("/api/culturing/"):
            # Per-symbol culturing ledger drill-down (state/culturing/<sym>.json,
            # written by forward_test_loop). Symbol names are alphanumeric but
            # sanitize to block path traversal.
            symbol = path.split("/api/culturing/", 1)[-1].strip("/")
            safe = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "" for ch in symbol)
            if not safe or safe in (".", ".."):
                self.send_error(404)
                return
            data = read_json_state(f"culturing/{safe}.json", default={})
            if not data:
                self.send_error(404)
                return
            self._send_json(data)
        elif path.startswith("/api/profit_quality/stream"):
            return _handle_sse_profit_quality(self)
        elif path == "/api/profit_quality":
            # Dedicated endpoint so the panel can poll on-demand without
            # dragging the full /api/state payload (which already includes the
            # same data). Useful where mobile/lite polls skip trade_log.json.
            # Wraps result in profit_quality + meter envelope to match the
            # expected schema of renderProfitQuality / renderPayoffParadoxMeter.
            _pq = _build_profit_quality(
                read_json_state("trade_log.json", default={}),
                read_json_state("position_management.json", default={}),
                read_json_state("trade_manager.json", default={}),
            )
            self._send_json({
                "profit_quality": _pq,
                "meter": _pq.get("meter", {}),
            })
        elif path == "/api/learning/timeline":
            self._send_json(_build_learning_timeline())
        elif path == "/api/cli_overrides":
            # Per-key provenance for every config.yaml / config.local.yaml /
            # active profile override. Independent of /api/state so a stale
            # state miss can't mask "where did this key come from?" — the
            # YAML walk is local and is bounded by the file's actual size.
            try:
                qs = parse_qs(urlparse(self.path).query or "")
                force = (qs.get("force", ["0"])[0] in ("1", "true", "yes"))
                self._send_json(_build_cli_overrides(force=force))
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("/api/cli_overrides failed: %s", exc)
                self._send_json({
                    "error": "cli_overrides_failed",
                    "message": str(exc)[:240],
                    "updated_at": utc_now_iso(),
                })
        elif path == "/api/daily_pnl":
            # Daily-profit-halt snapshot: today's closed-trade realized PnL,
            # threshold (config.risk.daily_profit_halt_usd), progress %, halt
            # status, and per-symbol breakdown. Independent from /api/state so
            # a stale state miss can't mask "$400 target reached" — the helper
            # reads paper_trades.json + kill_switch.json directly and is bounded
            # by their actual size.
            try:
                self._send_json(_build_daily_pnl_today())
            except Exception as _dxc:  # noqa: BLE001
                _LOG.warning("/api/daily_pnl failed: %s", _dxc)
                self._send_json({
                    "error": "daily_pnl_unavailable",
                    "message": str(_dxc)[:200],
                    "status": "error",
                    "updated_at_iso": utc_now_iso(),
                })
        elif path == "/api/adaptive_exit":
            self._send_json(_build_adaptive_exit_tile())
        elif path == "/api/living_params":
            self._send_json(_build_living_params())
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
        if path == "/api/reset-session":
            try:
                from scripts.reset_session_memory import reset_session_memory

                reset_session_memory()
                self._send_json({
                    "ok": True,
                    "message": "Session state reset — baselines, kill switch, and memory cleared",
                    "timestamp": utc_now_iso(),
                })
            except Exception as exc:
                self._send_json({"ok": False, "message": str(exc)}, 500)
        elif path == "/api/unblock-trades":
            try:
                # Clear all blocking gates so trades can flow immediately:
                # 1. Kill switch -> off (trading enabled)
                write_json_state("kill_switch.json", {
                    "kill_switch": False,
                    "reason": None,
                    "activated_at": None,
                    "cleared_at": utc_now_iso(),
                })
                # 2. Adaptive gates -> reset to normal tier, clear blocked symbols
                write_json_state("adaptive_gates.json", {
                    "timestamp": utc_now_iso(),
                    "enabled": True,
                    "tier": "normal",
                    "min_policy_score": 35.0,
                    "min_confidence": 50.0,
                    "blocked_symbols": [],
                    "lookback_n": 0,
                    "recent_win_rate_pct": 50.0,
                    "recent_net_pnl": 0.0,
                    "consecutive_losses": 0,
                    "reason": "manual unblock - all gates cleared",
                })
                # 3. Market-closed backoff -> clear all symbols
                write_json_state("market_closed_backoff.json", {
                    "timestamp": utc_now_iso(),
                    "symbols": {},
                })
                # 4. Learning overrides -> clear any live-apply patches
                write_json_state("learning_config_overrides.json", {
                    "timestamp": utc_now_iso(),
                    "patches": [],
                })
                self._send_json({
                    "ok": True,
                    "message": "All trade gates unblocked - kill switch cleared, adaptive gates reset to normal, market-closed backoff cleared",
                    "timestamp": utc_now_iso(),
                })
            except Exception as exc:
                self._send_json({"ok": False, "message": str(exc)}, 500)
        elif path == "/api/fast-mode":
            try:
                from core.fast_mode_runtime import (
                    FAST_MODE_PRESETS,
                    apply_overrides,
                    apply_preset,
                    clear_runtime,
                    read_runtime,
                )

                body = self._read_json_body()
                action = str(body.get("action") or "preset").lower()
                if action == "clear":
                    clear_runtime()
                    doc = read_runtime()
                elif action == "overrides":
                    overrides = dict(body.get("overrides") or {})
                    if not overrides:
                        self._send_json({"ok": False, "message": "overrides required"}, 400)
                        return
                    doc = apply_overrides(overrides, preset_id=body.get("preset"))
                else:
                    preset = str(body.get("preset") or "").lower()
                    if preset not in FAST_MODE_PRESETS:
                        self._send_json({
                            "ok": False,
                            "message": f"Unknown preset. Choose: {', '.join(FAST_MODE_PRESETS)}",
                        }, 400)
                        return
                    doc = apply_preset(preset, extra=body.get("extra"))
                self._send_json({
                    "ok": True,
                    "preset": doc.get("preset"),
                    "label": doc.get("label"),
                    "overrides": doc.get("overrides"),
                    "timestamp": doc.get("timestamp"),
                    "note": "Tick interval changes need a bot restart to take effect on the supervisor.",
                })
            except Exception as exc:
                self._send_json({"ok": False, "message": str(exc)}, 500)
        elif path == "/api/replay":
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
        elif path == "/api/switch_profile":
            try:
                body = self._read_json_body()
                profile = str(body.get("profile") or "").strip()
                if not profile:
                    self._send_json({"ok": False, "message": "Profile name required"}, 400)
                    return
                # Validate the profile file exists
                profile_path = ROOT / "profiles" / f"{profile}.yaml"
                if not profile_path.exists():
                    self._send_json({"ok": False, "message": f"Profile '{profile}' not found"}, 400)
                    return
                # Write switch request — start.py's supervisor checks this file
                write_json_state("profile_switch.json", {
                    "profile": profile,
                    "requested_at": utc_now_iso(),
                    "reason": f"Dashboard profile switch to {profile}",
                })
                # Launch new bot FIRST (independent process), then kill self
                # Order matters on Windows: os.kill is instant, so anything after
                # it would be dead code. The new bot takes 5-15s to boot (imports,
                # config), so port 8080 is free by the time it tries to bind.
                def _do_switch():
                    import subprocess as _sp
                    import time as _t
                    import signal as _sig
                    _t.sleep(0.5)  # brief pause so the API response is sent
                    # Launch new bot as an independent process
                    try:
                        _sp.Popen(
                            [sys.executable, "start.py", "--profile", profile],
                            cwd=str(ROOT),
                        )
                    except Exception:
                        pass
                    # NOW kill self — frees port 8080. New bot is already starting.
                    try:
                        os.kill(os.getpid(), _sig.SIGTERM)
                    except Exception:
                        pass
                threading.Thread(target=_do_switch, daemon=False).start()
                self._send_json({
                    "ok": True,
                    "message": f"Switching to profile '{profile}' — new bot starting, page will reload",
                    "profile": profile,
                    "timestamp": utc_now_iso(),
                })
            except Exception as exc:
                self._send_json({"ok": False, "message": str(exc)}, 500)
        elif path == "/api/reset_memory":
            try:
                # Reset learning state files
                write_json_state("learning_config_overrides.json", {
                    "patches": [],
                    "rollbacks": [],
                    "updated_at": utc_now_iso(),
                    "reset_by": "dashboard_memory_reset",
                })
                write_json_state("learning_state.json", {
                    "reviewed_count": 0,
                    "win_rate": 0.0,
                    "expectancy": 0.0,
                    "active_proposals": 0,
                    "applied_patches": 0,
                    "mistake_counts": {},
                    "reset_at": utc_now_iso(),
                    "reset_by": "dashboard_memory_reset",
                })
                write_json_state("trade_log.json", {
                    "trades": [],
                    "reset_at": utc_now_iso(),
                    "reset_by": "dashboard_memory_reset",
                })
                self._send_json({
                    "ok": True,
                    "message": "Learning memory reset — trade log, config patches, and learning state cleared",
                    "timestamp": utc_now_iso(),
                })
            except Exception as exc:
                self._send_json({"ok": False, "message": str(exc)}, 500)
        else:
            self.send_error(404)

    def log_message(self, format: str, *args) -> None:
        # Temporarily enabled (2026-07-02) to diagnose "phone page loads, data
        # not populated": capture each request the phone makes so we can see
        # the path/status/size it actually receives. Append-only, one line.
        try:
            import time as _t
            line = "%s %s\n" % (_t.strftime("%H:%M:%S"), (format % args))
            with (ROOT / "state" / "dashboard_access.log").open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        except Exception:
            pass


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
    # Start the Profit Quality SSE broadcaster daemon once at server boot so a
    # cold SSE client doesn't have to wait up to 5s for the first tick. Thread
    # is daemonized so it dies cleanly on process exit (KeyboardInterrupt or
    # normal shutdown). _ensure_loop is idempotent — the module-level
    # _SUBSCRIBERS list is empty until the first /api/profit_quality/stream
    # request arrives, so the loop iterates cheaply.
    _ProfitQualityBroadcaster._ensure_loop()
    server = ThreadingDashboardServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    import argparse as _ap

    _p = _ap.ArgumentParser(description="MT5 Quant OS dashboard server (reads state files directly)")
    _p.add_argument("--host", default=None, help="bind host (default 0.0.0.0, or DASH_HOST)")
    _p.add_argument("--port", type=int, default=None, help="bind port (default 8082, or DASH_PORT)")
    _a = _p.parse_args()
    run(host=_a.host, port=_a.port)
