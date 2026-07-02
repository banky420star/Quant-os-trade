"""Build a comprehensive per-trade log — the user's "whole works" trade record.

USER-AUTHORED feature request 2026-07-01: a clean trade log holding every field
per trade — opened/closed times, won/lost, setup, drawdown, entry/exit/SL/TP,
side, size, pnl, R-multiple, confidence, regime/session/bias, hold time, exit
reason. ``state/paper_trades.json`` already carries most of this, but two fields
are absent there:

  * ``opened_at`` — no open timestamp is stored on the closed-trade record.
  * drawdown / MAE / MFE — never tracked (the broker exits close-to-close).

This script fills both gaps by joining three sources:

  1. ``state/paper_trades.json`` — the closed-trade records (close time, pnl,
     result, exit, SL/TP, setup, confidence, market_context).
  2. ``state/paper_orders.json`` — the opening orders (volume/size, fill price,
     setup/confidence fallback) indexed by ``mt5_ticket``.
  3. MT5 deal history — the IN deal (open time + open price) and OUT deal
     (close time + close price + profit) for each position, joined by
     ``position_id`` == trade ``mt5_position``. This recovers ``opened_at`` for
     trades whose opening order is no longer retained (the archive-polluted
     set), not just the 75/283 that still have a paper_orders row.
  4. MT5 M5 candle range over each trade's [open, close] window — replayed to
     compute max-adverse-excursion (MAE = drawdown) and max-favorable-excursion
     (MFE) in both price and R (risk = |entry - sl|).

Read-only on MT5 (history_deals_get + copy_rates_range). Writes a single
self-contained file ``state/trade_log.json`` the dashboards/TUI read:

    {
      "updated_at", "total", "wins", "losses", "breakeven", "total_pnl",
      "avg_R", "expectancy_R", "trades": [ {one record per closed trade}, ... ]
    }

Run:
    python scripts/build_trade_log.py --dry-run   # print summary, write nothing
    python scripts/build_trade_log.py             # write state/trade_log.json
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore

from core.mt5_connection_manager import MT5ConnectionManager  # noqa: E402
from core.position_sync import _setup_type_from_comment  # noqa: E402
from core.symbol_manager import broker_symbol, logical_symbol  # noqa: E402
from core.trade_journal import (  # noqa: E402
    build_organized_index,
    build_symbol_journal_payload,
    enrich_trade_record,
    group_trades_by_symbol,
    safe_journal_name,
    safe_symbol_name,
    trade_detail_payload,
)
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state  # noqa: E402


def _build_ticket_index(orders_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """mt5 position ticket -> opening order. Mirrors Fix 1 / repair_archive."""
    index: dict[str, dict[str, Any]] = {}
    for o in (orders_state.get("orders") or []):
        tkt = o.get("mt5_ticket")
        if tkt:
            index[str(tkt)] = o
    return index


def _build_position_deals(deals, magic: int) -> tuple[dict[int, dict[str, Any]], dict[int, Any]]:
    """Return (position_id -> {in,out deals}, deal_ticket -> deal) for our magic."""
    by_pos: dict[int, dict[str, Any]] = {}
    by_ticket: dict[int, Any] = {}
    for d in deals:
        if d.magic != magic:
            continue
        by_ticket[int(d.ticket)] = d
        pos = getattr(d, "position_id", 0) or 0
        if not pos:
            continue
        slot = by_pos.setdefault(int(pos), {})
        if d.entry == mt5.DEAL_ENTRY_IN and "in" not in slot:
            slot["in"] = d
        elif d.entry == mt5.DEAL_ENTRY_OUT and "out" not in slot:
            slot["out"] = d
    return by_pos, by_ticket


def _iso_from_ts(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _hold_seconds(open_iso: str | None, close_iso: str | None) -> int | None:
    if not open_iso or not close_iso:
        return None
    try:
        o = datetime.fromisoformat(open_iso)
        c = datetime.fromisoformat(close_iso)
        return int((c - o).total_seconds())
    except Exception:  # noqa: BLE001
        return None


def _humanize_hold(sec: int | None) -> str:
    if sec is None:
        return "—"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60}s"
    h, rem = divmod(sec, 3600)
    return f"{h}h {rem // 60}m"


def _summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for r in trades if r.get("won"))
    losses = sum(1 for r in trades if r.get("result") == "loss")
    be = len(trades) - wins - losses
    total_pnl = sum((r.get("pnl") or 0) for r in trades)
    rs = [r["r_multiple"] for r in trades if r.get("r_multiple") is not None]
    avg_r = round(sum(rs) / len(rs), 3) if rs else None
    return {
        "total": len(trades),
        "wins": wins,
        "losses": losses,
        "breakeven": be,
        "win_rate_pct": round(100.0 * wins / max(wins + losses, 1), 1),
        "total_pnl": round(total_pnl, 2),
        "avg_R": avg_r,
        "expectancy_R": avg_r,
    }


def _session_trades(
    trades: list[dict[str, Any]],
    baseline: dict[str, Any],
) -> list[dict[str, Any]]:
    """Live-account trades since mt5_baseline.set_at, excluding archive pollution."""
    since = str(baseline.get("set_at") or "")
    clean = [r for r in trades if not r.get("archive_polluted")]
    if since:
        anchored = [r for r in clean if str(r.get("closed_at") or "") >= since]
        if anchored:
            return anchored
        # Baseline rebaseline mid-day — still show same-day live trades.
        day = since[:10]
        return [r for r in clean if str(r.get("closed_at") or "").startswith(day)]
    return clean


def _kelly_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the per-trade Kelly verdicts (sized-up vs. fallback)."""
    sized = 0
    fallback = 0
    cells_sized: dict[str, dict[str, Any]] = {}
    for t in trades:
        k = t.get("kelly")
        if not isinstance(k, dict):
            continue
        if k.get("gated"):
            fallback += 1
        else:
            sized += 1
            cell = k.get("cell") or "?"
            rec = cells_sized.setdefault(cell, {"n": 0, "fraction": k.get("fraction")})
            rec["n"] += 1
    return {
        "sized_trades": sized,
        "fallback_trades": fallback,
        "cells_sized_up": list(cells_sized.values()),
    }


def _compute_excursion(side: str, entry: float, bars) -> dict[str, Any]:
    """MAE/MFE in price + R from M5 bars over the trade window.

    MAE = max adverse excursion (drawdown from entry, always >= 0).
    MFE = max favorable excursion (run-up from entry, always >= 0).
    """
    out = {"mae_price": None, "mfe_price": None, "bars": 0}
    if bars is None or not len(bars):
        return out
    out["bars"] = int(len(bars))
    direction = 1.0 if str(side).upper() == "BUY" else -1.0
    # copy_rates_range returns a numpy structured array (field access, not attr).
    try:
        lows = [float(x) for x in bars["low"] if x]
        highs = [float(x) for x in bars["high"] if x]
    except Exception:  # noqa: BLE001 — fall back to per-row field access
        lows = [float(b["low"]) for b in bars if b["low"]]
        highs = [float(b["high"]) for b in bars if b["high"]]
    if not lows or not highs:
        return out
    min_low = min(lows)
    max_high = max(highs)
    if direction > 0:  # BUY: adverse = down (low), favorable = up (high)
        mae = entry - min_low
        mfe = max_high - entry
    else:  # SELL: adverse = up (high), favorable = down (low)
        mae = max_high - entry
        mfe = entry - min_low
    out["mae_price"] = round(max(mae, 0.0), 8)
    out["mfe_price"] = round(max(mfe, 0.0), 8)
    return out


def _r_metrics(side: str, entry: float, sl: float | None, exit_price: float | None,
               pnl: float | None, volume: float | None,
               sym_info: dict[str, Any] | None) -> dict[str, Any]:
    """R-multiple of the realized outcome + risk unit.

    Prefers the pnl-based R (broker deal profit is ground truth; the recorded
    ``exit`` price on older archive records is sometimes stale/wrong, which
    flips the sign of a price-based R). Dollar risk = (|entry-sl| / tick_size)
    * tick_value * volume, all from ``mt5.symbol_info``. Falls back to the
    price-based R (direction * (exit-entry) / |entry-sl|) when symbol info or
    pnl is unavailable.
    """
    risk_price = abs(entry - sl) if (entry is not None and sl is not None) else 0.0
    if not risk_price:
        return {"risk_price": None, "r_multiple": None, "risk_amount": None}
    # Implausible-SL guard: a stop within 0.02% of entry is almost certainly a
    # break-even/trailing artifact, not the initial risk. Emitting R off it would
    # produce absurd values (e.g. -188R). Flag as unknown rather than mislead.
    if entry and risk_price < 0.0002 * abs(entry):
        return {"risk_price": round(risk_price, 8), "r_multiple": None,
                "risk_amount": None, "sl_implausible": True}

    risk_amount = None
    if sym_info and volume:
        ts = float(sym_info.get("trade_tick_size") or 0.0)
        tv = float(sym_info.get("trade_tick_value") or 0.0)
        if ts > 0 and tv > 0:
            ticks = risk_price / ts
            risk_amount = round(ticks * tv * float(volume), 8)

    # pnl-based R (ground truth) when dollar risk is known.
    if risk_amount and pnl is not None:
        return {"risk_price": round(risk_price, 8), "risk_amount": risk_amount,
                "r_multiple": round(float(pnl) / risk_amount, 3)}

    # Fallback: price-based R from the fill price (may diverge from pnl on
    # records with a stale exit).
    direction = 1.0 if str(side).upper() == "BUY" else -1.0
    r = (direction * (exit_price - entry) / risk_price) if exit_price is not None else None
    return {"risk_price": round(risk_price, 8), "risk_amount": risk_amount,
            "r_multiple": (round(r, 3) if r is not None else None)}


def build_log(config: dict[str, Any], days: int, log) -> dict[str, Any]:
    """Join paper_trades + paper_orders + MT5 deals/candles -> trade log."""
    magic = int(config.get("execution", {}).get("magic_number", 20250625))

    trades_state = read_json_state("paper_trades.json", default={"trades": []})
    trades = trades_state.get("trades", []) if isinstance(trades_state, dict) else []
    log.info("paper_trades: %d closed records", len(trades))

    orders_state = read_json_state("paper_orders.json", default={})
    ticket_index = _build_ticket_index(orders_state)
    log.info("paper_orders index: %d orders by mt5_ticket", len(ticket_index))

    pos_deals: dict[int, dict[str, Any]] = {}
    deal_by_ticket: dict[int, Any] = {}
    sym_info_cache: dict[str, dict[str, Any] | None] = {}
    if mt5 is not None:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        deals = mt5.history_deals_get(since, datetime.now(timezone.utc)) or []
        pos_deals, deal_by_ticket = _build_position_deals(deals, magic)
        log.info("MT5 deals: %d positions, %d deals by ticket (magic=%s)", len(pos_deals), len(deal_by_ticket), magic)
    else:
        log.warning("MetaTrader5 not installed — opened_at/drawdown from MT5 unavailable.")

    def _sym_info(symbol: str) -> dict[str, Any] | None:
        """Cached mt5.symbol_info tick_size/tick_value for dollar-risk R."""
        if symbol is None:
            return None
        broker = broker_symbol(logical_symbol(symbol))
        if broker in sym_info_cache:
            return sym_info_cache[broker]
        info = None
        if mt5 is not None:
            try:
                si = mt5.symbol_info(broker)
                if si is not None:
                    info = {
                        "trade_tick_size": getattr(si, "trade_tick_size", 0.0),
                        "trade_tick_value": getattr(si, "trade_tick_value", 0.0),
                        "contract_size": getattr(si, "trade_contract_size", 0.0),
                    }
            except Exception:  # noqa: BLE001
                info = None
        sym_info_cache[broker] = info
        return info

    out_trades: list[dict[str, Any]] = []
    n_open = n_dd = 0
    for t in trades:
        sym = logical_symbol(str(t.get("symbol") or ""))
        side = t.get("side") or ""
        entry = t.get("entry")
        sl = t.get("sl")
        tp1 = t.get("tp1")
        tp2 = None
        volume = None
        pos = t.get("mt5_position")
        deals_for = pos_deals.get(int(pos)) if pos is not None else None
        in_deal = (deals_for or {}).get("in")
        out_deal = (deals_for or {}).get("out")
        # Fallback for archive-polluted trades with no mt5_position: resolve the
        # OUT deal by its ticket, read its position_id, then the matching IN deal.
        if in_deal is None and deal_by_ticket and t.get("mt5_deal") is not None:
            out_by_tkt = deal_by_ticket.get(int(t["mt5_deal"]))
            if out_by_tkt is not None:
                if out_deal is None:
                    out_deal = out_by_tkt
                pos2 = getattr(out_by_tkt, "position_id", 0) or 0
                if pos2:
                    in_deal = (pos_deals.get(int(pos2)) or {}).get("in")
        order = ticket_index.get(str(pos)) if pos is not None else None
        # Per-cell Kelly verdict stamped on the order at sizing time (may be
        # absent on older archive trades placed before the Kelly wiring).
        kelly = ((order or {}).get("signal_meta") or {}).get("kelly") if order else None

        # Initial SL (the risk actually taken at entry) — prefer the opening
        # order's SL; the trade record's ``sl`` may have been dragged to entry
        # by a break-even/trailing move, which makes the R-multiple explode
        # (e.g. a 0.001-wide oil stop -> -188R). Fall back to the trade's sl.
        sl_initial = (order or {}).get("sl") if order else None
        sl_for_risk = sl_initial if sl_initial else sl

        # opened_at: prefer MT5 IN deal, fall back to opening order created_at.
        opened_at = None
        open_price = None
        if in_deal is not None:
            opened_at = _iso_from_ts(in_deal.time)
            open_price = float(in_deal.price) if in_deal.price else None
            n_open += 1
            if in_deal.comment and (not t.get("setup_type") or t.get("setup_type") == "unknown"):
                t = {**t, "setup_type": _setup_type_from_comment(in_deal.comment)}
        elif order and order.get("created_at"):
            opened_at = order.get("created_at")
            open_price = order.get("fill_price") or order.get("entry")
            n_open += 1

        # closed_at: prefer the record (already enriched), fall back to OUT deal.
        closed_at = t.get("closed_at")
        if not closed_at and out_deal is not None:
            closed_at = _iso_from_ts(out_deal.time)
        # close_price: prefer the true broker fill (OUT deal price) — the
        # recorded ``exit`` on older archive records can be stale and disagree
        # with the deal profit (which is ground truth). Keep the recorded exit
        # alongside for transparency.
        exit_recorded = t.get("exit")
        close_price = None
        if out_deal is not None and out_deal.price:
            close_price = float(out_deal.price)
        if close_price is None:
            close_price = exit_recorded
        if open_price and (not entry or entry == close_price):
            entry = open_price

        if volume is None:
            volume = (order or {}).get("volume") if order else None
        if volume is None and out_deal is not None:
            volume = float(getattr(out_deal, "volume", 0.0)) or None

        # R-multiple + risk (pnl-based with per-symbol dollar risk; price-based fallback).
        r = _r_metrics(side, float(entry) if entry is not None else 0.0, sl_for_risk, close_price,
                       t.get("pnl"), volume, _sym_info(sym))

        setup = t.get("setup_type")
        if (not setup or setup == "unknown") and order:
            setup = (order or {}).get("setup_type") or ((order or {}).get("signal_meta") or {}).get("setup_type")
        if (not setup or setup == "unknown") and out_deal is not None and out_deal.comment:
            setup = _setup_type_from_comment(out_deal.comment)

        # Drawdown / MAE / MFE from M5 bars over the trade window.
        dd = {"mae_price": None, "mfe_price": None, "mae_R": None, "mfe_R": None, "bars": 0}
        broker_sym = broker_symbol(sym)
        if mt5 is not None and broker_sym and opened_at and closed_at and entry is not None:
            try:
                o_dt = datetime.fromisoformat(opened_at)
                c_dt = datetime.fromisoformat(closed_at)
                if not mt5.symbol_select(broker_sym, True):
                    raise RuntimeError(f"symbol_select failed for {broker_sym}")
                bars = mt5.copy_rates_range(broker_sym, mt5.TIMEFRAME_M5, o_dt, c_dt)
                exc = _compute_excursion(side, float(entry), bars)
                dd["mae_price"] = exc["mae_price"]
                dd["mfe_price"] = exc["mfe_price"]
                dd["bars"] = exc["bars"]
                if r["risk_price"] and exc["mae_price"] is not None:
                    dd["mae_R"] = round(exc["mae_price"] / r["risk_price"], 3)
                if r["risk_price"] and exc["mfe_price"] is not None:
                    dd["mfe_R"] = round(exc["mfe_price"] / r["risk_price"], 3)
                if exc["mae_price"] is not None:
                    n_dd += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("rates range failed for %s pos=%s: %s", sym, pos, exc)

        mc = t.get("market_context") if isinstance(t.get("market_context"), dict) else {}
        mr = mc.get("market_regime") if isinstance(mc.get("market_regime"), dict) else {}
        hold_s = _hold_seconds(opened_at, closed_at)

        raw_snapshot = dict(t)
        if isinstance(t.get("signal_meta"), dict):
            raw_snapshot.update({k: v for k, v in t["signal_meta"].items() if k not in raw_snapshot})

        rec = {
            "trade_id": t.get("trade_id"),
            "signal_id": t.get("signal_id"),
            "symbol": sym,
            "side": side,
            "setup": setup or t.get("setup_type") or "unknown",
            "result": t.get("result"),
            "won": t.get("result") == "win",
            "pnl": t.get("pnl"),
            "r_multiple": r["r_multiple"],
            "risk_price": r["risk_price"],
            "risk_amount": r.get("risk_amount"),
            "entry": entry,
            "exit": close_price,
            "exit_recorded": exit_recorded,
            "sl": sl,
            "sl_initial": sl_initial,
            "tp1": tp1,
            "tp2": tp2,
            "volume": volume,
            "confidence": t.get("confidence"),
            "opened_at": opened_at,
            "closed_at": closed_at,
            "hold_seconds": hold_s,
            "hold_human": _humanize_hold(hold_s),
            "open_price": open_price,
            "exit_reason": t.get("exit_reason"),
            "reason": t.get("reason"),
            "market_intent": mc.get("market_intent") or t.get("market_intent"),
            "regime_primary": mr.get("primary") or mc.get("regime"),
            "regime_bias": mr.get("bias"),
            "session": mc.get("session"),
            "phase": mc.get("phase"),
            "move_type": mc.get("move_type") or t.get("move_type"),
            "tags": mr.get("tags"),
            "archive_polluted": bool(t.get("archive_polluted")),
            "mt5_position": pos,
            "mt5_deal": t.get("mt5_deal"),
            "mae_price": dd["mae_price"],
            "mfe_price": dd["mfe_price"],
            "mae_R": dd["mae_R"],
            "mfe_R": dd["mfe_R"],
            "drawdown_bars": dd["bars"],
            "kelly": kelly,
            "be_narrative": t.get("be_narrative"),
            "trail_narrative": t.get("trail_narrative"),
            "exit_narrative": t.get("exit_narrative"),
            "be_triggered": t.get("be_triggered"),
            "trail_active": t.get("trail_active"),
            "position_mgmt": t.get("position_mgmt"),
            "signal_meta": (order or {}).get("signal_meta") or t.get("signal_meta"),
        }
        enrich_trade_record(rec, order=order, raw_trade=raw_snapshot)
        out_trades.append(rec)

    # Most recent first.
    out_trades.sort(key=lambda r: r.get("closed_at") or "", reverse=True)

    baseline = read_json_state("mt5_baseline.json", default={}) or {}
    session_list = _session_trades(out_trades, baseline)
    session_stats = _summarize_trades(session_list)
    all_stats = _summarize_trades(out_trades)

    log.info(
        "Trade log: %d trades (%d session), %d wins, %d losses | opened_at=%d, drawdown=%d | session_pnl=%.2f avg_R=%s",
        all_stats["total"],
        session_stats["total"],
        session_stats["wins"],
        session_stats["losses"],
        n_open,
        n_dd,
        session_stats["total_pnl"],
        session_stats["avg_R"],
    )

    configured_symbols = [
        str(s) for s in (config.get("mt5", {}).get("symbols") or [])
        if s
    ]
    organized = build_organized_index(out_trades, configured_symbols=configured_symbols)
    sym_groups = group_trades_by_symbol(out_trades)
    journal_symbols = 0
    journal_written = 0
    now = utc_now_iso()
    for sym in organized.get("symbols") or sorted(sym_groups.keys()):
        safe_sym = safe_symbol_name(sym)
        sym_trades = sym_groups.get(sym, [])
        sym_payload = build_symbol_journal_payload(sym, sym_trades)
        sym_payload["updated_at"] = now
        write_json_state(f"trade_journal/symbols/{safe_sym}.json", sym_payload)
        journal_symbols += 1
        for rec in sym_trades:
            tid = rec.get("trade_id")
            if not tid:
                continue
            body = trade_detail_payload(rec)
            body["updated_at"] = now
            safe_tid = safe_journal_name(str(tid))
            write_json_state(f"trade_journal/symbols/{safe_sym}/{safe_tid}.json", body)
            # Flat path kept for backward-compatible API lookups.
            write_json_state(f"trade_journal/{safe_tid}.json", body)
            journal_written += 1
    log.info(
        "Trade journal: %d symbols, %d per-trade detail files",
        journal_symbols,
        journal_written,
    )

    return {
        "updated_at": utc_now_iso(),
        "schema_version": 3,
        **all_stats,
        "opened_at_known": n_open,
        "drawdown_known": n_dd,
        "kelly": _kelly_summary(session_list or out_trades),
        "organized": organized,
        "journal_files": journal_written,
        "journal_symbols": journal_symbols,
        "symbols": organized.get("symbols") or [],
        "session": {
            "login": baseline.get("login"),
            "since": baseline.get("set_at"),
            **session_stats,
        },
        "trades": out_trades,
        "session_trades": session_list,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build comprehensive per-trade log")
    parser.add_argument("--dry-run", action="store_true", help="Print summary; write nothing")
    parser.add_argument("--days", type=int, default=30, help="MT5 deal/candle lookback days")
    args = parser.parse_args()

    log = setup_logger("build_trade_log", "build_trade_log.log")
    config = load_config()

    if mt5 is not None:
        conn = MT5ConnectionManager(config, log)
        if not conn.connect():
            log.error("MT5 connect failed; proceeding with paper-only fields (no opened_at/drawdown from MT5).")
            conn = None
    else:
        conn = None

    try:
        payload = build_log(config, args.days, log)
    finally:
        if conn is not None:
            try:
                mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass

    if args.dry_run:
        log.info("DRY RUN — no file written. Summary: %d trades, %d wins/%d losses, pnl=%.2f, avg_R=%s, opened=%d, drawdown=%d",
                 payload["total"], payload["wins"], payload["losses"], payload["total_pnl"], payload["avg_R"],
                 payload["opened_at_known"], payload["drawdown_known"])
    else:
        write_json_state("trade_log.json", payload)
        log.info("Wrote state/trade_log.json (%d trades).", payload["total"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())