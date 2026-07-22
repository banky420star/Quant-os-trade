"""Replay Engine — backtest agent logic bar-by-bar on Parquet history."""

from __future__ import annotations

import copy
import logging
from typing import Any

import pandas as pd

from core.bocpd_gate import BocpdDetector, log_return as _bocpd_log_return
from core.decision_engine import DecisionEngine
from core.edge_database import EdgeDatabase
from core.feature_engine import FeatureEngine
from core.history_manager import HistoryManager
from core.market_context import MarketContextEngine
from core.market_regime import MarketRegimeEngine
from core.memory_engine import MemoryEngine
from core.paper_broker import PaperBroker
from core.trade_enrichment import enrich_trades
from core.utils import utc_now_iso, write_json_state
from core.growth_replay import GrowthReplayTracker
from core.news_calendar import news_context_at
from core.verifier import Verifier


class ReplayEngine:
    """Walk historical candles and simulate full agent pipeline in paper mode."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = copy.deepcopy(config)
        self.logger = logger or logging.getLogger("replay_engine")
        self.replay_cfg = self.config.get("replay", {})
        self.config["execution"]["mode"] = "paper"
        self.history = HistoryManager(self.config, logger=self.logger)

    def run(
        self,
        symbol: str | None = None,
        max_bars: int | None = None,
        step: int | None = None,
    ) -> dict[str, Any]:
        symbol = symbol or self.replay_cfg.get("symbol") or self.config["mt5"]["symbols"][0]
        max_bars = max_bars or int(self.replay_cfg.get("max_bars", 500))
        step = step or int(self.replay_cfg.get("bars_per_step", 5))
        min_bars = int(self.config.get("features", {}).get("min_bars_required", 20))

        m5_df = self.history.load(symbol, "M5")
        m15_df = self.history.load(symbol, "M15")
        if m5_df is None or m5_df.empty:
            raise RuntimeError(f"No M5 history for {symbol}")

        m5_df = m5_df.sort_values("time").reset_index(drop=True)
        if m15_df is not None and not m15_df.empty:
            m15_df = m15_df.sort_values("time").reset_index(drop=True)

        start_idx = max(min_bars, len(m5_df) - max_bars)
        feature_engine = FeatureEngine(self.config, history_manager=None, logger=self.logger)
        ctx_engine = MarketContextEngine(self.logger)
        regime_engine = MarketRegimeEngine(self.logger)
        decision = DecisionEngine(self.config, self.logger)
        verifier = Verifier(self.config, self.logger)
        broker = PaperBroker(self.config, self.logger)
        memory = MemoryEngine(self.logger)
        edge_db = EdgeDatabase(self.logger)
        ingest_edge = bool(self.config.get("quant", {}).get("replay_ingest_edge_db", True))

        orders: list[dict] = []
        positions: list[dict] = []
        trades: list[dict] = []
        approved_history: list[dict] = []
        balance = {
            "cash": float(self.config["execution"].get("starting_cash", 1000)),
            "equity": float(self.config["execution"].get("starting_cash", 1000)),
            "starting_cash": float(self.config["execution"].get("starting_cash", 1000)),
        }
        edge_scores: dict[str, Any] = {"setups": {}, "setup_stats": {}}
        memory_state: dict[str, Any] = {"records": [], "adjustments": []}
        signals_generated = 0
        signals_approved = 0
        news_blocked = 0
        growth_blocked = 0
        growth_tracker = GrowthReplayTracker(self.config, balance.get("equity", balance["cash"]))

        for i in range(start_idx, len(m5_df), step):
            m5_slice = m5_df.iloc[: i + 1]
            bar_time = m5_slice["time"].iloc[-1]
            m15_slice = self._m15_up_to(m15_df, bar_time) if m15_df is not None else pd.DataFrame()

            if len(m5_slice) < min_bars:
                continue
            if m15_slice is not None and len(m15_slice) < min_bars:
                continue

            candles = {
                "source": "replay",
                "symbols": {
                    symbol: {
                        "M5": m5_slice.to_dict("records"),
                        "M15": m15_slice.to_dict("records") if len(m15_slice) else [],
                    }
                },
            }
            features = feature_engine.compute_all(candles)
            if symbol not in features.get("symbols", {}):
                continue

            context = ctx_engine.analyze_all(features, candles)
            regimes = regime_engine.classify_all(features, context)
            for sym, ctx in context.get("symbols", {}).items():
                ctx["market_regime"] = regimes.get("symbols", {}).get(sym, {})

            price = features["symbols"][symbol]["price"]
            prices = {symbol: price}
            # Intrabar high/low over the step window (last `step` bars) so the
            # broker sees intrabar stop-runs / trailing whipsaw, not only close.
            tail = m5_slice.tail(step)
            bar_highlow = {symbol: (float(tail["high"].max()), float(tail["low"].min()))} if len(tail) else None
            candidates = decision.generate_candidates(features, context, edge_scores)
            signals_generated += len(candidates)

            growth_tracker.on_bar(bar_time, balance.get("equity", balance["cash"]))
            if growth_tracker.should_block_entries():
                growth_blocked += len(candidates)
                candidates = []
            else:
                news_ctx = news_context_at(bar_time, self.config)
                if not news_ctx.get("safe_for_entry", True):
                    news_blocked += len(candidates)
                    candidates = []

            approved, _rejected = verifier.verify_batch(
                candidates,
                features,
                active_signals=positions,
                spread_data={symbol: 0.0},
                equity=balance.get("equity", balance["cash"]),
                closed_trades=trades,
                reference_time=bar_time,
            )
            signals_approved += len(approved)

            if approved:
                approved_history.extend(approved)

            prior_trade_count = len(trades)
            result = broker.process_approved_signals(
                approved,
                prices,
                orders,
                positions,
                trades,
                balance,
                bar_highlow=bar_highlow,
            )
            orders = result["orders"]
            positions = result["positions"]
            trades = result["trades"]
            balance = result["balance"]

            if ingest_edge and approved and len(trades) > prior_trade_count:
                new_closed = trades[prior_trade_count:]
                enriched = enrich_trades(
                    new_closed,
                    approved_data={"approved": approved_history},
                    orders=orders,
                )
                edge_db.ingest_batch(
                    enriched,
                    features=features,
                    context=context,
                    source="replay",
                )

            mem_out = memory.process(trades, features, context, memory_state, edge_scores)
            memory_state = mem_out["memory"]
            edge_scores = mem_out["edge_scores"]

        wins = sum(1 for t in trades if t.get("result") == "win")
        losses = len(trades) - wins
        pnl = sum(float(t.get("pnl", 0)) for t in trades)

        output = {
            "timestamp": utc_now_iso(),
            "symbol": symbol,
            "bars_replayed": len(range(start_idx, len(m5_df), step)),
            "signals_generated": signals_generated,
            "signals_approved": signals_approved,
            "trades_closed": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else 0,
            "pnl_total": round(pnl, 2),
            "final_equity": round(balance.get("equity", 0), 2),
            "starting_cash": balance.get("starting_cash"),
            "trades": trades[-50:],
            "setup_stats": edge_scores.get("setup_stats", {}),
            "news_blocked_signals": news_blocked,
            "growth_blocked_signals": growth_blocked,
            "growth_replay": growth_tracker.summary(balance.get("equity", 0)),
        }
        write_json_state("replay_results.json", output)
        self.logger.info(
            "Replay %s: %d bars, %d trades, PnL=%.2f, win rate=%.1f%%",
            symbol,
            output["bars_replayed"],
            len(trades),
            pnl,
            output["win_rate_pct"],
        )
        return output

    @staticmethod
    def _m15_up_to(m15_df: pd.DataFrame | None, bar_time: Any) -> pd.DataFrame:
        if m15_df is None or m15_df.empty:
            return pd.DataFrame()
        # M5/M15 parquets are timestamped at bar OPEN. The M5 decision at
        # ``bar_time`` resolves the M5 bar's CLOSE (bar_time + 5min). An M15 bar
        # opened at t closes at t+15min; it is only KNOWN-closed by the decision
        # time if t+15 <= bar_time+5, i.e. t <= bar_time-10min. Including the
        # in-progress M15 bar would leak ~10min of its future OHLC into the
        # trend/alignment feature (look-ahead). Exclude it.
        # The parquet stores `time` as fixed-width tz-aware ISO strings
        # ('...+00:00'); for that format lexicographic order == chronological
        # (the M5 str slice at line ~316 already relies on this). Compute the
        # -10min cutoff as an ISO string and compare str-vs-str -- fast (no
        # per-step pd.to_datetime on the whole column) and type-preserving (the
        # records we hand downstream keep their original str `time` values).
        cutoff = (pd.Timestamp(bar_time) - pd.Timedelta(minutes=10)).isoformat()
        return m15_df[m15_df["time"] <= cutoff]


def run_portfolio_replay(
    config: dict[str, Any],
    symbols: list[str],
    *,
    max_bars: int | None = None,
    step: int | None = None,
    logger: logging.Logger | None = None,
    spread_data: dict[str, float] | None = None,
    return_all_trades: bool = False,
) -> dict[str, Any]:
    """Replay all symbols on one timeline with shared equity and exposure.

    ``spread_data`` (points per symbol) is forwarded to the verifier's spread
    gate so wide-spread bars can reject candidates as they would live. ``None``
    preserves the legacy zero-spread behaviour. ``return_all_trades`` returns
    every closed trade (needed by the strategy evaluator for stats) instead of
    the last 50.
    """
    log = logger or logging.getLogger("portfolio_replay")
    cfg = copy.deepcopy(config)
    cfg["execution"]["mode"] = "paper"
    replay_cfg = cfg.get("replay", {})
    max_bars = max_bars or int(replay_cfg.get("max_bars", 500))
    step = step or int(replay_cfg.get("bars_per_step", 5))
    min_bars = int(cfg.get("features", {}).get("min_bars_required", 20))

    history = HistoryManager(cfg, logger=log)
    m5_by_symbol: dict[str, pd.DataFrame] = {}
    m15_by_symbol: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        m5_df = history.load(symbol, "M5")
        if m5_df is None or m5_df.empty:
            raise RuntimeError(f"No M5 history for {symbol}")
        m5_by_symbol[symbol] = m5_df.sort_values("time").reset_index(drop=True)
        m15_df = history.load(symbol, "M15")
        m15_by_symbol[symbol] = (
            m15_df.sort_values("time").reset_index(drop=True)
            if m15_df is not None and not m15_df.empty
            else pd.DataFrame()
        )

    ref_symbol = symbols[0]
    ref_m5 = m5_by_symbol[ref_symbol]
    start_idx = max(min_bars, len(ref_m5) - max_bars)
    timeline_indices = list(range(start_idx, len(ref_m5), step))
    bars_replayed = len(timeline_indices)

    feature_engine = FeatureEngine(cfg, history_manager=None, logger=log)
    ctx_engine = MarketContextEngine(log)
    regime_engine = MarketRegimeEngine(log)
    decision = DecisionEngine(cfg, log)
    verifier = Verifier(cfg, log)
    broker = PaperBroker(cfg, log)
    memory = MemoryEngine(log)
    edge_db = EdgeDatabase(log)
    ingest_edge = bool(cfg.get("quant", {}).get("replay_ingest_edge_db", True))

    orders: list[dict] = []
    positions: list[dict] = []
    trades: list[dict] = []
    approved_history: list[dict] = []
    starting_cash = float(cfg["execution"].get("starting_cash", 1000))
    balance = {"cash": starting_cash, "equity": starting_cash, "starting_cash": starting_cash}
    edge_scores: dict[str, Any] = {"setups": {}, "setup_stats": {}}
    memory_state: dict[str, Any] = {"records": [], "adjustments": []}
    signals_generated = 0
    signals_approved = 0
    news_blocked = 0
    growth_blocked = 0
    growth_tracker = GrowthReplayTracker(cfg, starting_cash)

    # --- Optional pre-registered BOCPD change-point gate (OFF by default). ---
    # When ``regime_gating/bocpd_enabled`` is true, a causal Adams-MacKay
    # detector per symbol is updated online with M5 log-returns; candidate
    # signals generated on bars flagged as changepoint/transition are dropped
    # BEFORE verifier approval. Default false -> zero behaviour change. See
    # ``core/bocpd_gate.py`` for the pre-registration rationale.
    rg_cfg = cfg.get("regime_gating", {}) or {}
    bocpd_enabled = bool(rg_cfg.get("bocpd_enabled", False))
    skip_cp_bars = bool(rg_cfg.get("skip_changepoint_bars", True))
    # --- Optional pre-registered SESSION gate (OFF by default). ---
    # When ``regime_gating/session_gate_enabled`` is true, candidate signals are
    # dropped unless the current bar's UTC hour is within
    # [session_start_utc, session_end_utc). Defaults (12, 16) = the London/NY
    # overlap already pre-registered in core/session_scorer.py as the tightest
    # spread XAU window, so this adds ~0 new DSR trials. Bar times are stored as
    # tz-aware UTC ISO STRINGS (core/data_collector.py:113 writes
    # ``datetime.fromtimestamp(..., tz=utc).isoformat()``), so the session gate
    # parses them via ``pd.Timestamp(bar_time).hour`` to get the UTC hour.
    session_gate_enabled = bool(rg_cfg.get("session_gate_enabled", False))
    session_start_utc = int(rg_cfg.get("session_start_utc", 12))
    session_end_utc = int(rg_cfg.get("session_end_utc", 16))
    session_dropped = 0
    bocpd_detectors: dict[str, BocpdDetector] = {}
    bocpd_prev_close: dict[str, float] = {}
    bocpd_last_idx: dict[str, int] = {}
    bocpd_dropped = 0
    if bocpd_enabled:
        for sym in symbols:
            bocpd_detectors[sym] = BocpdDetector(
                hazard_lambda=float(rg_cfg.get("hazard_lambda", 430.0)),
                cp_max_run=int(rg_cfg.get("cp_max_run", 1)),
                transition_max_run=int(rg_cfg.get("transition_max_run", 5)),
                warmup_bars=int(rg_cfg.get("warmup_bars", 200)),
            )
            bocpd_prev_close[sym] = 0.0
            bocpd_last_idx[sym] = -1

    for i in timeline_indices:
        bar_time = ref_m5["time"].iloc[i]
        symbol_candles: dict[str, dict[str, list]] = {}
        for symbol in symbols:
            m5_df = m5_by_symbol[symbol]
            m5_slice = m5_df[m5_df["time"] <= bar_time]
            if len(m5_slice) < min_bars:
                continue
            m15_df = m15_by_symbol.get(symbol)
            m15_slice = ReplayEngine._m15_up_to(m15_df, bar_time) if m15_df is not None else pd.DataFrame()
            if m15_slice is not None and len(m15_slice) < min_bars:
                continue
            symbol_candles[symbol] = {
                "M5": m5_slice.to_dict("records"),
                "M15": m15_slice.to_dict("records") if len(m15_slice) else [],
            }

        if not symbol_candles:
            continue

        candles = {"source": "portfolio_replay", "symbols": symbol_candles}
        features = feature_engine.compute_all(candles)
        if not features.get("symbols"):
            continue

        context = ctx_engine.analyze_all(features, candles)
        regimes = regime_engine.classify_all(features, context)
        for sym, ctx in context.get("symbols", {}).items():
            ctx["market_regime"] = regimes.get("symbols", {}).get(sym, {})

        prices = {sym: feat["price"] for sym, feat in features["symbols"].items()}
        candidates = decision.generate_candidates(features, context, edge_scores)
        signals_generated += len(candidates)

        # --- SESSION gate: drop candidates whose current bar UTC hour is outside
        # --- the pre-registered overlap window. Only runs when the knob is on.
        if session_gate_enabled and candidates:
            # bar_time is an ISO-format STRING (ref_m5["time"] is stored as
            # tz-aware UTC ISO strings per core/data_collector.py:113), so it
            # has no .hour attribute -- parse via pd.Timestamp. The prior
            # ``bar_time.hour`` raised AttributeError every call, fell to the
            # ``bar_hour = -1`` branch, and dropped EVERY candidate (the
            # session_gate variant silently zero-traded instead of filtering
            # to the 12-16 UTC overlap). pd.Timestamp(str) preserves the UTC
            # offset and .hour returns the UTC hour.
            try:
                bar_hour = int(pd.Timestamp(bar_time).hour)
            except (AttributeError, TypeError, ValueError):
                bar_hour = -1
            if not (session_start_utc <= bar_hour < session_end_utc):
                session_dropped += len(candidates)
                candidates = []

        # --- BOCPD gate: causally update per-symbol detector with EVERY M5
        # --- log-return since the last step (so the detector runs on M5 bars,
        # --- not step-scale returns — keeping hazard lambda in M5-bar units),
        # --- then drop candidates whose symbol's CURRENT bar is flagged as a
        # --- changepoint/transition. Only runs when the knob is on.
        if bocpd_enabled and skip_cp_bars and candidates:
            for sym in prices:
                df_sym = m5_by_symbol[sym]
                slice_sym = df_sym[df_sym["time"] <= bar_time]
                closes = slice_sym["close"].astype(float).tolist()
                prev_close = bocpd_prev_close.get(sym, 0.0)
                start = bocpd_last_idx.get(sym, -1) + 1
                # Feed every M5 close since the last update so the detector
                # sees one update per M5 bar (lambda=430 stays in M5-bar
                # units, warmup_bars=200 completes within ~200 M5 bars).
                for idx in range(start, len(closes)):
                    cur_close = closes[idx]
                    if prev_close > 0.0:
                        ret = _bocpd_log_return(prev_close, cur_close)
                        bocpd_detectors[sym].update(ret)
                    prev_close = cur_close
                bocpd_prev_close[sym] = prev_close
                bocpd_last_idx[sym] = len(closes) - 1
            kept: list[dict[str, Any]] = []
            for cand in candidates:
                sym = cand.get("symbol")
                det = bocpd_detectors.get(sym)
                if det is not None and det.is_gated_bar():
                    bocpd_dropped += 1
                    continue
                kept.append(cand)
            candidates = kept

        growth_tracker.on_bar(bar_time, balance.get("equity", balance["cash"]))
        if growth_tracker.should_block_entries():
            growth_blocked += len(candidates)
            candidates = []
        elif candidates:
            news_ctx = news_context_at(bar_time, cfg)
            if not news_ctx.get("safe_for_entry", True):
                news_blocked += len(candidates)
                candidates = []

        approved, _rejected = verifier.verify_batch(
            candidates,
            features,
            active_signals=positions,
            spread_data=spread_data or {sym: 0.0 for sym in prices},
            equity=balance.get("equity", balance["cash"]),
            closed_trades=trades,
            reference_time=bar_time,
        )
        signals_approved += len(approved)
        if approved:
            approved_history.extend(approved)

        # Intrabar high/low over the step window (last `step` M5 bars) so the
        # broker's exit check sees intrabar stop-runs / trailing whipsaw rather
        # than only the close. This fixes the close-to-close exit bias.
        bar_highlow: dict[str, tuple[float, float]] = {}
        for sym in prices:
            df = m5_by_symbol[sym]
            tail = df[df["time"] <= bar_time].tail(step)
            if len(tail):
                bar_highlow[sym] = (float(tail["high"].max()), float(tail["low"].min()))

        prior_trade_count = len(trades)
        result = broker.process_approved_signals(
            approved,
            prices,
            orders,
            positions,
            trades,
            balance,
            bar_highlow=bar_highlow,
        )
        orders = result["orders"]
        positions = result["positions"]
        trades = result["trades"]
        balance = result["balance"]

        if approved and len(trades) > prior_trade_count:
            # Enrich newly closed trades with their entry-time signal context
            # (regime / session / setup_type / confidence) so downstream callers
            # — the win-condition learner — can see the CONDITIONS that surrounded
            # each trade. Splice the enriched copies back into the master list.
            enriched = enrich_trades(
                trades[prior_trade_count:],
                approved_data={"approved": approved_history},
                orders=orders,
            )
            trades[prior_trade_count:] = enriched
            if ingest_edge:
                edge_db.ingest_batch(
                    enriched,
                    features=features,
                    context=context,
                    source="portfolio_replay",
                )

        mem_out = memory.process(trades, features, context, memory_state, edge_scores)
        memory_state = mem_out["memory"]
        edge_scores = mem_out["edge_scores"]

    symbol_results: list[dict[str, Any]] = []
    total_pnl = 0.0
    for symbol in symbols:
        sym_trades = [t for t in trades if t.get("symbol") == symbol]
        wins = sum(1 for t in sym_trades if t.get("result") == "win")
        pnl = sum(float(t.get("pnl", 0)) for t in sym_trades)
        total_pnl += pnl
        symbol_results.append({
            "symbol": symbol,
            "bars_replayed": bars_replayed,
            "trades_closed": len(sym_trades),
            "wins": wins,
            "losses": len(sym_trades) - wins,
            "win_rate_pct": round(wins / len(sym_trades) * 100, 1) if sym_trades else 0,
            "pnl_total": round(pnl, 2),
        })

    wins_total = sum(1 for t in trades if t.get("result") == "win")
    output = {
        "timestamp": utc_now_iso(),
        "symbols": symbols,
        "bars_replayed": bars_replayed,
        "signals_generated": signals_generated,
        "signals_approved": signals_approved,
        "bocpd_dropped": bocpd_dropped,
        "session_dropped": session_dropped,
        "trades_closed": len(trades),
        "wins": wins_total,
        "losses": len(trades) - wins_total,
        "win_rate_pct": round(wins_total / len(trades) * 100, 1) if trades else 0,
        "pnl_total": round(total_pnl, 2),
        "final_equity": round(balance.get("equity", 0), 2),
        "cash": round(balance.get("cash", 0), 2),
        # Unrealized PnL of positions still open at replay end (can be either
        # sign -- an open position may be underwater). Exposed so callers/tests
        # can assert the true accounting identity final_equity = cash +
        # unrealized_open, rather than the false assumption unrealized >= 0.
        "unrealized_open": round(balance.get("equity", 0) - balance.get("cash", 0), 2),
        "starting_cash": starting_cash,
        "symbol_results": symbol_results,
        "trades": trades if return_all_trades else trades[-50:],
        "setup_stats": edge_scores.get("setup_stats", {}),
        "news_blocked_signals": news_blocked,
        "growth_blocked_signals": growth_blocked,
        "growth_replay": growth_tracker.summary(balance.get("equity", 0)),
    }
    log.info(
        "Portfolio replay: %d bars, %d trades, PnL=%.2f across %d symbols",
        bars_replayed,
        len(trades),
        total_pnl,
        len(symbols),
    )
    return output