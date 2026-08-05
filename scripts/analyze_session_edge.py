"""Summarize what worked vs failed from session trade/order data."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.trade_history import trade_history_filename, trade_orders_filename  # noqa: E402
from core.utils import load_config  # noqa: E402


def load(name: str) -> dict:
    with open(ROOT / "state" / name, encoding="utf-8") as f:
        return json.load(f)


def enrich_trades(trades: list, orders: list) -> list[dict]:
    """Join closed trades back to their opening order's signal_meta.

    The order ledger (mt5_orders.json in MT5 mode) is the only place the full
    setup/session/regime/confidence context survives; the closed-trade record
    drops most of it. Trades link to orders via ``mt5_position`` == order
    ``mt5_ticket`` (and, for archive records, via the deal ticket). Session
    lives at ``signal_meta.session`` (top level), not nested under
    ``market_context`` — the latter is empty on real orders.
    """
    from core.session_scorer import resolve_trading_session
    from core.specialized_shadow_analyzer import session_from_utc_hour

    by_pos: dict[int, dict] = {}
    by_deal: dict[int, dict] = {}
    for order in orders:
        if order.get("status") != "filled":
            continue
        sm = order.get("signal_meta") or {}
        mctx = sm.get("market_context") if isinstance(sm.get("market_context"), dict) else {}
        sess = sm.get("session") or mctx.get("session")
        opened_at = order.get("filled_at") or order.get("created_at")
        hour = None
        try:
            if opened_at:
                hour = int(pd.Timestamp(opened_at).tz_convert("UTC").hour)
        except Exception:  # noqa: BLE001
            hour = None
        meta = {
            "setup_type": order.get("setup_type") or sm.get("setup_type"),
            "side": order.get("side"),
            "symbol": order.get("symbol"),
            "confidence": sm.get("confidence"),
            "regime": mctx.get("market_regime", {}).get("primary") or sm.get("regime_primary"),
            "bias": mctx.get("market_regime", {}).get("bias") or sm.get("regime_bias"),
            "session": sess or (resolve_trading_session(hour) if hour is not None else None),
            "utc_hour": hour,
            "opened_at": opened_at,
        }
        if order.get("mt5_ticket") is not None:
            by_pos[int(order["mt5_ticket"])] = meta
        if order.get("mt5_deal") is not None:
            by_deal[int(order["mt5_deal"])] = meta

    enriched = []
    for trade in trades:
        row = dict(trade)
        pos = trade.get("mt5_position")
        meta = by_pos.get(int(pos)) if pos is not None else None
        if meta is None:
            deal = trade.get("mt5_deal") or trade.get("trade_id")
            meta = by_deal.get(int(deal)) if deal is not None else None
        if meta:
            row.update(meta)
        if row.get("utc_hour") is None and row.get("closed_at"):
            # No opening order (archive) — fall back to the CLOSE hour so the
            # trade still lands in a bucket for coarse evaluation. This is an
            # ESTIMATE (an entry hour is what actually matters); mark it so the
            # report can distinguish real entry-hour attribution from this.
            try:
                row["utc_hour"] = int(pd.Timestamp(row["closed_at"]).tz_convert("UTC").hour)
                row["session_source"] = "close_fallback"
            except Exception:  # noqa: BLE001
                pass
        else:
            row["session_source"] = "order"
        if row.get("utc_hour") is not None and not row.get("session"):
            row["session"] = session_from_utc_hour(row["utc_hour"])
        enriched.append(row)
    return enriched


def bucket_stats(rows: list[dict], key_fn, label: str) -> list[tuple]:
    buckets: dict[str, dict] = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for row in rows:
        key = key_fn(row) or "unknown"
        if not isinstance(key, str):
            key = str(key)
        buckets[key]["n"] += 1
        buckets[key]["pnl"] += float(row.get("pnl") or 0)
        if row.get("result") == "win":
            buckets[key]["w"] += 1
    out = []
    for key, val in buckets.items():
        wr = 100 * val["w"] / val["n"] if val["n"] else 0
        out.append((val["pnl"], key, val["n"], wr, val["w"]))
    out.sort()
    print(f"\n--- {label} (worst -> best) ---")
    for pnl, key, n, wr, w in out:
        print(f"  {key:32} n={n:4} wr={wr:5.1f}% pnl={pnl:9.2f}")
    return out


def main() -> None:
    # Read the ACTIVE execution mode's ledgers (mt5_* in MT5 mode, paper_* in
    # paper mode) so this analysis reflects the trades the bot is actually
    # placing. Hardcoded paper_* left the MT5 mode report empty.
    config = load_config()
    trades = load(trade_history_filename(config)).get("trades", [])
    orders = load(trade_orders_filename(config)).get("orders", [])
    rankings = load("strategy_rankings.json").get("rankings", {})
    enriched = enrich_trades(trades, orders)

    total_pnl = sum(float(t.get("pnl") or 0) for t in enriched)
    wins = [t for t in enriched if t.get("result") == "win"]
    losses = [t for t in enriched if t.get("result") == "loss"]

    print("=== CLOSED TRADES (joined to order metadata) ===")
    print(f"count={len(enriched)} wins={len(wins)} losses={len(losses)} net_pnl={total_pnl:.2f}")
    if wins:
        print(f"avg_win={sum(t['pnl'] for t in wins) / len(wins):.2f}")
    if losses:
        print(f"avg_loss={sum(t['pnl'] for t in losses) / len(losses):.2f}")

    with_meta = [t for t in enriched if t.get("setup_type") and not str(t["setup_type"]).startswith("[sl")]
    print(f"trades_with_real_setup_type={len(with_meta)}/{len(enriched)}")
    n_close_fb = sum(1 for t in enriched if t.get("session_source") == "close_fallback")
    n_order = sum(1 for t in enriched if t.get("session_source") == "order")
    print(
        f"session attribution: {n_order} from opening order (entry hour), "
        f"{n_close_fb} close-hour fallback (archive, approximate), "
        f"{len(enriched) - n_order - n_close_fb} unknown"
    )

    bucket_stats(enriched, lambda t: t.get("symbol"), "symbol")
    bucket_stats(enriched, lambda t: f"{t.get('symbol')}/{t.get('side')}", "symbol+side")
    if with_meta:
        bucket_stats(with_meta, lambda t: t.get("setup_type"), "setup_type")
        bucket_stats(with_meta, lambda t: f"{t.get('symbol')}/{t.get('setup_type')}", "symbol+setup")
        bucket_stats(
            with_meta,
            lambda t: f"{int(t.get('confidence') or 0) // 10 * 10}-{int(t.get('confidence') or 0) // 10 * 10 + 9}",
            "confidence_band",
        )
        bucket_stats(with_meta, lambda t: t.get("regime"), "regime")
        bucket_stats(with_meta, lambda t: t.get("session"), "session")
        bucket_stats(
            with_meta,
            lambda t: f"utc{int(t.get('utc_hour')):02d}:00" if t.get("utc_hour") is not None else "hour?",
            "utc_hour (what time works best)",
        )
        bucket_stats(
            with_meta,
            lambda t: f"{t.get('setup_type')}@{t.get('session')}"
            + ("*" if t.get("session_source") == "close_fallback" else "")
            if t.get("session") else t.get("setup_type"),
            "setup@session (best setup per time window; * = close-hour estimate)",
        )

    filled = [o for o in orders if o.get("status") == "filled"]
    print("\n=== FILLED ORDERS (what we kept opening) ===")
    print(f"total_filled={len(filled)}")
    bucket_stats(
        filled,
        lambda o: o.get("setup_type") or o.get("signal_meta", {}).get("setup_type"),
        "opened setup_type",
    )
    bucket_stats(
        filled,
        lambda o: f"{o.get('symbol')}/{o.get('side')}",
        "opened symbol+side",
    )

    print("\n=== STRATEGY RANKINGS (from session memory) ===")
    for symbol, rows in rankings.items():
        print(f"\n{symbol}:")
        for row in rows:
            print(
                f"  #{row.get('rank')} {row.get('setup_type'):20} "
                f"wr={row.get('win_rate_pct')}% n={row.get('total')} score={row.get('score')}"
            )


if __name__ == "__main__":
    main()