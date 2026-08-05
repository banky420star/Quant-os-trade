"""Fast tick loop — tick-reactive entry layer (observe-only by default).

Persistent MT5 connection (2026-07-29): ``_MT5_CONN`` is cached at module
level so fast_tick_loop does NOT open and close an MT5 session on every
1-second tick. Instead it reuses one connection across calls, only
reconnecting when ``ping()`` reveals the session died or a new config
is loaded. This eliminates the IPC contention + per-tick latency that
was producing transient account reads and false ``insufficient_margin``
errors (diagnosed in the 2026-07-29 runtime audit).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.adaptive_symbol_learner import record_tick_snapshot
from core.audit_log import append_event
from core.fast_entry_executor import evaluate_entry
from core.fast_mode import fast_mode_enabled, fast_mode_live, fast_mode_settings, fast_mode_symbols
from core.fast_signal_cache import prune_expired, read_cache, read_fast_state
from core.mt5_connection_manager import MT5ConnectionManager
from core.state_store import get_state_store, state_store_enabled
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state

_LOGGER = None

# Persistent MT5 connection: cached across tick cycles so we don't
# reconnect on every 1-second tick. Created on first use; reconnected
# automatically when the session dies.
_MT5_CONN: MT5ConnectionManager | None = None
_MT5_CONN_LOCK: threading.Lock | None = None


def _logger():
    global _LOGGER
    if _LOGGER is None:
        _LOGGER = setup_logger("fast_tick_loop", "fast_tick_loop.log")
    return _LOGGER


def _get_mt5_connection(config: dict, logger) -> MT5ConnectionManager | None:
    """Return the cached MT5 connection, reconnecting if dead or new config."""
    global _MT5_CONN, _MT5_CONN_LOCK
    if _MT5_CONN_LOCK is None:
        _MT5_CONN_LOCK = threading.Lock()
    with _MT5_CONN_LOCK:
        if _MT5_CONN is not None:
            if not _MT5_CONN.connected:
                # Cached facade was released (e.g. _tick_prices disconnected on
                # a read failure). Recreate instead of returning the stale
                # facade forever — otherwise live ticks never recover.
                _MT5_CONN = None
            else:
                try:
                    ping = _MT5_CONN.ping()
                    if ping.get("alive") and ping.get("logged_in"):
                        return _MT5_CONN
                except Exception:
                    pass
                # Session died — the shared MT5Owner re-establishes it
                # (owner-owned shutdown + re-init). Never per-worker disconnect:
                # disconnect() is a reference release that does NOT re-initialize
                # a dead session.
                try:
                    if _MT5_CONN.reconnect():
                        return _MT5_CONN
                except Exception:
                    pass
                _MT5_CONN = None

        if _MT5_CONN is None:
            conn = MT5ConnectionManager(config, logger)
            try:
                conn.connect()
                _MT5_CONN = conn
                logger.info(
                    "Persistent MT5 connection established (login=%s balance=%.2f)",
                    conn.account_snapshot().get("login"),
                    conn.account_snapshot().get("balance", 0),
                )
            except Exception as exc:
                logger.warning("Persistent MT5 connect failed: %s — using features", exc)
                return None
        return _MT5_CONN


def _close_mt5_connection() -> None:
    """Explicitly close the persistent MT5 connection (used on shutdown)."""
    global _MT5_CONN, _MT5_CONN_LOCK
    if _MT5_CONN_LOCK is None:
        return
    with _MT5_CONN_LOCK:
        if _MT5_CONN is not None:
            try:
                _MT5_CONN.disconnect()
            except Exception:
                pass
            _MT5_CONN = None


def _write_decisions(doc: dict, config: dict) -> None:
    write_json_state("fast_mode_decisions.json", doc)
    store = get_state_store(config)
    if store and state_store_enabled(config):
        store.set_kv("fast_mode_decisions", doc)
        for row in doc.get("decisions") or []:
            store.append_fast_decision(row)


def _tick_prices(config, symbols: list[str], logger) -> dict[str, dict]:
    """Fetch bid/ask/spread per symbol.

    Observe-only mode uses the features snapshot (no MT5 connect per tick).
    Live fast mode hits MT5 for tick-accurate entry triggers.
    """
    prices: dict[str, dict] = {}
    features = read_json_state("features.json", default={"symbols": {}})
    mode = config.get("execution", {}).get("mode", "paper")
    use_mt5_ticks = mode == "mt5" and fast_mode_live(config)

    if use_mt5_ticks:
        conn = _get_mt5_connection(config, logger)
        if conn is not None:
            try:
                from core.mt5_client import MT5Client

                client = MT5Client(config, conn, logger)
                for sym in symbols:
                    tick = client.get_current_price(sym)
                    spread = client.get_spread_points(sym)
                    if tick:
                        prices[sym] = {
                            "mid": tick["mid"],
                            "bid": tick["bid"],
                            "ask": tick["ask"],
                            "spread_points": spread,
                        }
            except Exception as exc:
                logger.warning("Fast tick MT5 read failed: %s — using features", exc)
                # Connection may be dead — force reconnect on next tick
                try:
                    if conn.connected:
                        conn.disconnect()
                except Exception:
                    pass
        else:
            logger.warning("No MT5 connection available — using features.json prices")

        if not prices:
            logger.debug("MT5 returned no prices, falling back to features")

    for sym in symbols:
        if sym in prices:
            continue
        feat = (features.get("symbols") or {}).get(sym) or {}
        close = float(feat.get("close") or feat.get("last_close") or feat.get("price") or 0)
        spread = float(feat.get("spread_points") or feat.get("spread") or 0)
        if close > 0:
            half = spread * float(feat.get("point") or 0.01) / 2 if spread else 0
            prices[sym] = {
                "mid": close,
                "bid": close - half,
                "ask": close + half,
                "spread_points": spread,
            }
    return prices


def run(config: dict | None = None) -> dict | None:
    if config is None:
        config = load_config()
    logger = _logger()

    if not fast_mode_enabled(config):
        return None

    cfg = fast_mode_settings(config)
    symbols = fast_mode_symbols(config)
    cache = prune_expired(read_cache(config))
    cache_symbols = dict(cache.get("symbols") or {})
    # Sample every configured fast symbol, even when no evaluated signal is
    # cached. Learning must not stop merely because the entry cache is empty.
    observation_target = symbols or list(cache_symbols.keys())
    observation_prices = _tick_prices(config, observation_target, logger)
    observation_features = read_json_state("features.json", default={"symbols": {}})
    try:
        record_tick_snapshot(config, observation_prices, observation_features)
    except Exception as exc:
        logger.warning("Adaptive tick observation failed: %s", exc)

    if not cache_symbols:
        logger.debug("Fast tick: empty cache")
        doc = {
            "timestamp": utc_now_iso(),
            "mode": "live" if cfg.get("live_enabled") else "observe",
            "decisions": [],
            "cache_symbols": 0,
            "observed_symbols": len(observation_prices),
        }
        _write_decisions(doc, config)
        return doc

    target = [s for s in symbols if s in cache_symbols] if symbols else list(cache_symbols.keys())
    prices = observation_prices if target == observation_target else _tick_prices(config, target, logger)
    features = observation_features
    state = read_fast_state()
    # 2026-08-05 — merge the mt5_orders ledger into the fast state's executed
    # set once per tick, so slow-pipeline-executed signals are also deduped by
    # evaluate_entry (no per-symbol file read) and stop the FAST LIVE entry
    # spam / duplicate re-attempts.
    try:
        _orders_doc = read_json_state("mt5_orders.json", default={"orders": []})
        _exec_sig = set(state.get("executed_signals") or [])
        for _o in (_orders_doc.get("orders") or []):
            if _o.get("status") in ("filled", "pending", "placed") and _o.get("signal_id"):
                _exec_sig.add(_o["signal_id"])
        state["executed_signals"] = sorted(_exec_sig)
    except Exception as _merge_exc:
        logger.debug("executed-signal merge skipped: %s", _merge_exc)
    decisions: list[dict] = []

    for sym in target:
        entry = cache_symbols.get(sym)
        if not entry:
            continue
        px = prices.get(sym)
        if not px:
            decisions.append({
                "timestamp": utc_now_iso(),
                "symbol": sym,
                "action": "blocked",
                "reasons": ["no_price"],
            })
            continue
        feat = (features.get("symbols") or {}).get(sym) or {}
        dec = evaluate_entry(
            entry,
            mid=float(px["mid"]),
            spread_points=float(px.get("spread_points") or 0),
            feat=feat,
            config=config,
            state=state,
            logger=logger,
        )
        if dec.get("action", "").startswith("enter"):
            from core.fast_live_executor import execute_fast_entry

            # 2026-08-05 — single-producer fix: the direct execute_fast_entry
            # path is authoritative (verifier approval, kill switch, executed-
            # id dedupe, ledger persistence, trade-entry recording). The old
            # push_intent here queued a SECOND order that execution_loop's
            # drain phase placed ~35s later — the same signal was opened twice
            # (30 duplicate groups in trade_log.json). Intents are only for
            # non-fast producers now (web API / guard ratchets); the fast layer
            # executes synchronously.
            exec_result = execute_fast_entry(
                dec,
                entry,
                config=config,
                state=state,
                logger=logger,
            )
            dec["execution"] = exec_result
            if exec_result.get("blocked"):
                dec["action"] = "blocked"
                dec["reasons"] = list(dec.get("reasons") or []) + [exec_result.get("reason", "execution_blocked")]
            elif not exec_result.get("success") and not exec_result.get("skipped"):
                dec["action"] = "entry_failed"
                dec["reasons"] = list(dec.get("reasons") or []) + [exec_result.get("error", "order_failed")]

        decisions.append(dec)
        if dec.get("action", "").startswith(("enter", "would_enter", "entry_failed")):
            append_event(
                "fast_mode.entry",
                symbol=sym,
                details={
                    "action": dec["action"],
                    "live": dec.get("live"),
                    "reasons": dec.get("reasons"),
                    "execution": dec.get("execution"),
                },
            )

    doc = {
        "timestamp": utc_now_iso(),
        "mode": "live" if cfg.get("live_enabled") else "observe",
        "tick_interval_ms": cfg.get("tick_interval_ms"),
        "cache_symbols": len(cache_symbols),
        "decisions": decisions,
    }
    _write_decisions(doc, config)
    logger.info(
        "Fast tick: %d symbols, %d decisions (mode=%s)",
        len(target),
        len(decisions),
        doc["mode"],
    )
    return doc


if __name__ == "__main__":
    run()