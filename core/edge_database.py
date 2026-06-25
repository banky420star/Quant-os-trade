"""Edge Database — rich trade metadata for quant research."""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from typing import Any

from core.setup_library import SETUP_LIBRARY
from core.utils import read_json_state, utc_now_iso, write_json_state

MAX_RECORDS = 50_000


class EdgeDatabase:
    """Store and query trades with full market context for statistical edge."""

    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger("edge_database")

    def load(self) -> dict[str, Any]:
        return read_json_state("edge_database.json", default={"records": [], "aggregates": {}})

    def save(self, data: dict[str, Any]) -> None:
        write_json_state("edge_database.json", data)

    def ingest_trade(
        self,
        trade: dict[str, Any],
        *,
        features: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        signal: dict[str, Any] | None = None,
        source: str = "live",
    ) -> dict[str, Any] | None:
        """Append a rich edge record; skip duplicates by trade_id."""
        db = self.load()
        records: list[dict] = list(db.get("records", []))
        known = {r.get("trade_id") for r in records}
        tid = trade.get("trade_id")
        if not tid or tid in known:
            return None

        record = self._build_edge_record(trade, features, context, signal, source)
        records.append(record)
        if len(records) > MAX_RECORDS:
            records = records[-MAX_RECORDS:]

        db["records"] = records
        db["count"] = len(records)
        db["timestamp"] = utc_now_iso()
        db["aggregates"] = self._compute_aggregates(records)
        self.save(db)
        self.logger.debug("Edge DB ingest: %s %s %s", record["setup_type"], record["result"], record["market_regime"])
        return record

    def ingest_batch(
        self,
        trades: list[dict[str, Any]],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Ingest multiple trades in one read/write cycle (safe for replay)."""
        if not trades:
            return []

        db = self.load()
        records: list[dict] = list(db.get("records", []))
        known = {r.get("trade_id") for r in records}
        added: list[dict[str, Any]] = []

        for trade in trades:
            tid = trade.get("trade_id")
            if not tid or tid in known:
                continue
            record = self._build_edge_record(
                trade,
                kwargs.get("features"),
                kwargs.get("context"),
                kwargs.get("signal"),
                kwargs.get("source", "live"),
            )
            records.append(record)
            known.add(tid)
            added.append(record)

        if not added:
            return []

        if len(records) > MAX_RECORDS:
            records = records[-MAX_RECORDS:]

        db["records"] = records
        db["count"] = len(records)
        db["timestamp"] = utc_now_iso()
        db["aggregates"] = self._compute_aggregates(records)
        self.save(db)
        self.logger.info("Edge DB: ingested %d new records (total context-rich)", len(added))
        return added

    def query_win_rate(
        self,
        *,
        setup_type: str | None = None,
        market_regime: str | None = None,
        session: str | None = None,
        symbol: str | None = None,
        volatility: str | None = None,
        min_samples: int = 1,
    ) -> dict[str, Any]:
        """Query win rate for a dimensional slice."""
        records = self.load().get("records", [])
        filtered = records
        if setup_type:
            filtered = [r for r in filtered if r.get("setup_type") == setup_type]
        if market_regime:
            filtered = [r for r in filtered if r.get("market_regime") == market_regime]
        if session:
            filtered = [r for r in filtered if r.get("session") == session]
        if symbol:
            filtered = [r for r in filtered if r.get("symbol") == symbol]
        if volatility:
            filtered = [r for r in filtered if r.get("volatility") == volatility]

        total = len(filtered)
        if total < min_samples:
            return {"total": total, "wins": 0, "losses": 0, "win_rate_pct": 0, "avg_rr": 0, "avg_pnl": 0, "insufficient": True}

        wins = sum(1 for r in filtered if r.get("result") == "win")
        losses = total - wins
        rr_vals = [r["rr"] for r in filtered if r.get("rr")]
        pnl_vals = [r["pnl"] for r in filtered if r.get("pnl") is not None]
        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / total * 100, 1),
            "avg_rr": round(sum(rr_vals) / len(rr_vals), 2) if rr_vals else 0,
            "avg_pnl": round(sum(pnl_vals) / len(pnl_vals), 2) if pnl_vals else 0,
            "insufficient": False,
        }

    def rank_setups_for_context(
        self,
        symbol: str,
        market_regime: str,
        session: str,
        volatility: str = "normal",
        min_samples: int = 3,
    ) -> list[dict[str, Any]]:
        """Rank setup types by historical win rate under matching conditions."""
        aggregates = self.load().get("aggregates", {})
        key = f"{symbol}|{market_regime}|{session}"
        cell = aggregates.get("by_context", {}).get(key, {})

        rankings: list[dict[str, Any]] = []
        for setup, stats in cell.items():
            if setup not in SETUP_LIBRARY:
                continue
            if stats.get("total", 0) < min_samples:
                score = 50.0
                insufficient = True
            else:
                score = float(stats.get("win_rate_pct", 0))
                insufficient = False
            rankings.append({
                "setup_type": setup,
                "score": round(score, 1),
                "win_rate_pct": stats.get("win_rate_pct", 0),
                "total": stats.get("total", 0),
                "avg_rr": stats.get("avg_rr", 0),
                "insufficient_data": insufficient,
            })

        if not rankings:
            for setup in (
                "trend_continuation", "pullback", "breakout", "range_fade",
                "liquidity_sweep", "mean_reversion", "false_breakout", "compression_breakout",
            ):
                global_stats = self.query_win_rate(setup_type=setup, market_regime=market_regime, min_samples=min_samples)
                if global_stats["total"] >= min_samples:
                    rankings.append({
                        "setup_type": setup,
                        "score": global_stats["win_rate_pct"],
                        "win_rate_pct": global_stats["win_rate_pct"],
                        "total": global_stats["total"],
                        "avg_rr": global_stats["avg_rr"],
                        "insufficient_data": False,
                    })

        rankings.sort(key=lambda x: (x["score"], x["total"]), reverse=True)
        return rankings

    def _build_edge_record(
        self,
        trade: dict[str, Any],
        features: dict[str, Any] | None,
        context: dict[str, Any] | None,
        signal: dict[str, Any] | None,
        source: str,
    ) -> dict[str, Any]:
        symbol = trade.get("symbol")
        feat = (features or {}).get("symbols", {}).get(symbol, {})
        ctx = {}
        if context:
            ctx = context.get("symbols", {}).get(symbol, context.get("market_context", {}).get("symbols", {}).get(symbol, {}))

        meta = signal or trade.get("signal_meta") or {}
        mctx = meta.get("market_context", trade.get("market_context", {}))
        regime = mctx.get("market_regime", {})
        entry = float(trade.get("entry") or meta.get("entry") or 0)
        exit_p = float(trade.get("exit") or 0)
        sl = float(trade.get("sl") or meta.get("sl") or 0)
        tp1 = float(trade.get("tp1") or meta.get("tp1") or 0)
        risk = abs(entry - sl) if sl else 0
        reward = abs(tp1 - entry) if tp1 else abs(exit_p - entry)
        rr = round(reward / risk, 2) if risk > 0 else 0

        return {
            "id": str(uuid.uuid4()),
            "trade_id": trade.get("trade_id"),
            "signal_id": trade.get("signal_id") or meta.get("signal_id"),
            "source": source,
            "symbol": symbol,
            "side": trade.get("side"),
            "setup_type": trade.get("setup_type") or meta.get("setup_type"),
            "market_regime": regime.get("primary") or mctx.get("regime") or ctx.get("regime"),
            "session": mctx.get("session") or ctx.get("session"),
            "volatility": feat.get("volatility_regime", "normal"),
            "phase": mctx.get("phase") or ctx.get("phase"),
            "spread_points": feat.get("spread_points", feat.get("spread", 0)),
            "confidence": meta.get("confidence", trade.get("confidence")),
            "confidence_tree": meta.get("confidence_tree", trade.get("confidence_tree", {})),
            "evidence": meta.get("evidence", trade.get("evidence", {})),
            "entry": entry,
            "exit": exit_p,
            "sl": sl,
            "tp1": tp1,
            "rr": rr,
            "pnl": trade.get("pnl"),
            "result": trade.get("result"),
            "exit_reason": trade.get("exit_reason"),
            "m5_trend": feat.get("m5_trend"),
            "m15_trend": feat.get("m15_trend"),
            "closed_at": trade.get("closed_at"),
            "recorded_at": utc_now_iso(),
        }

    def _compute_aggregates(self, records: list[dict]) -> dict[str, Any]:
        by_setup: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0, "rr_sum": 0.0, "pnl_sum": 0.0})
        by_context: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0, "rr_sum": 0.0}))
        by_session_setup: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0}))

        for rec in records:
            setup = rec.get("setup_type", "unknown")
            result = rec.get("result")
            by_setup[setup]["total"] += 1
            by_setup[setup]["rr_sum"] += float(rec.get("rr") or 0)
            by_setup[setup]["pnl_sum"] += float(rec.get("pnl") or 0)
            if result == "win":
                by_setup[setup]["wins"] += 1
            else:
                by_setup[setup]["losses"] += 1

            ctx_key = f"{rec.get('symbol')}|{rec.get('market_regime')}|{rec.get('session')}"
            cell = by_context[ctx_key][setup]
            cell["total"] += 1
            cell["rr_sum"] = cell.get("rr_sum", 0) + float(rec.get("rr") or 0)
            if result == "win":
                cell["wins"] += 1
            else:
                cell["losses"] += 1

            sess_key = f"{rec.get('session')}|{setup}"
            by_session_setup[sess_key][setup]["total"] += 1
            if result == "win":
                by_session_setup[sess_key][setup]["wins"] += 1

        def _finalize(bucket: dict) -> dict:
            out = {}
            for k, s in bucket.items():
                wr = round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0
                out[k] = {
                    **s,
                    "win_rate_pct": wr,
                    "avg_rr": round(s.get("rr_sum", 0) / s["total"], 2) if s["total"] else 0,
                    "avg_pnl": round(s.get("pnl_sum", 0) / s["total"], 2) if s.get("pnl_sum") else 0,
                }
            return out

        ctx_out = {}
        for ctx_key, setups in by_context.items():
            ctx_out[ctx_key] = _finalize(setups)

        return {
            "by_setup": _finalize(by_setup),
            "by_context": ctx_out,
            "total_records": len(records),
        }