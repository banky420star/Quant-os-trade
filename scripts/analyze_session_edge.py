"""Summarize what worked vs failed from session trade/order data."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name: str) -> dict:
    with open(ROOT / "state" / name, encoding="utf-8") as f:
        return json.load(f)


def enrich_trades(trades: list, orders: list) -> list[dict]:
    by_deal: dict[int, dict] = {}
    by_ticket: dict[int, dict] = {}
    for order in orders:
        if order.get("status") != "filled":
            continue
        meta = {
            "setup_type": order.get("setup_type")
            or order.get("signal_meta", {}).get("setup_type"),
            "side": order.get("side"),
            "symbol": order.get("symbol"),
            "confidence": order.get("signal_meta", {}).get("confidence"),
            "regime": order.get("signal_meta", {})
            .get("market_context", {})
            .get("market_regime", {})
            .get("primary"),
            "bias": order.get("signal_meta", {})
            .get("market_context", {})
            .get("market_regime", {})
            .get("bias"),
            "session": order.get("signal_meta", {})
            .get("market_context", {})
            .get("session"),
        }
        if order.get("mt5_deal") is not None:
            by_deal[int(order["mt5_deal"])] = meta
        if order.get("mt5_ticket") is not None:
            by_ticket[int(order["mt5_ticket"])] = meta

    enriched = []
    for trade in trades:
        row = dict(trade)
        deal = trade.get("mt5_deal") or trade.get("trade_id")
        meta = by_deal.get(int(deal)) if deal is not None else None
        if meta:
            row.update(meta)
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
    trades = load("paper_trades.json").get("trades", [])
    orders = load("paper_orders.json").get("orders", [])
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

    bucket_stats(enriched, lambda t: t.get("symbol"), "symbol")
    bucket_stats(enriched, lambda t: f"{t.get('symbol')}/{t.get('side')}", "symbol+side")
    if with_meta:
        bucket_stats(with_meta, lambda t: t.get("setup_type"), "setup_type")
        bucket_stats(with_meta, lambda t: f"{t.get('symbol')}/{t.get('setup_type')}", "symbol+setup")
        bucket_stats(
            with_meta,
            lambda t: f"{t.get('confidence', 0) // 10 * 10}-{t.get('confidence', 0) // 10 * 10 + 9}",
            "confidence_band",
        )
        bucket_stats(with_meta, lambda t: t.get("regime"), "regime")
        bucket_stats(with_meta, lambda t: t.get("session"), "session")

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