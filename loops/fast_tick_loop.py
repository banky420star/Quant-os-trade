"""Fast tick loop — tick-reactive entry layer (observe-only by default)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.audit_log import append_event
from core.fast_entry_executor import evaluate_entry
from core.fast_mode import fast_mode_enabled, fast_mode_live, fast_mode_settings, fast_mode_symbols
from core.fast_signal_cache import prune_expired, read_cache, read_fast_state
from core.mt5_connection_manager import MT5ConnectionManager
from core.state_store import get_state_store, state_store_enabled
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state

_LOGGER = None


def _logger():
    global _LOGGER
    if _LOGGER is None:
        _LOGGER = setup_logger("fast_tick_loop", "fast_tick_loop.log")
    return _LOGGER


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
        conn = MT5ConnectionManager(config, logger)
        try:
            conn.connect()
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
        finally:
            try:
                conn.disconnect()
            except Exception:
                pass

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
    if not cache_symbols:
        logger.debug("Fast tick: empty cache")
        doc = {
            "timestamp": utc_now_iso(),
            "mode": "live" if cfg.get("live_enabled") else "observe",
            "decisions": [],
            "cache_symbols": 0,
        }
        _write_decisions(doc, config)
        return doc

    target = [s for s in symbols if s in cache_symbols] if symbols else list(cache_symbols.keys())
    prices = _tick_prices(config, target, logger)
    features = read_json_state("features.json", default={"symbols": {}})
    state = read_fast_state()
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
            try:
                from core.mt5_terminal_manager import MT5TerminalManager
                _mgr = MT5TerminalManager(config, logger)
                _side = (
                    dec.get("side")
                    or (entry.get("side") if isinstance(entry, dict) else None)
                    or "BUY"
                )
                _mgr.push_intent({
                    "action": "open",
                    "symbol": sym,
                    "side": _side,
                    "signal_id": (
                        entry.get("signal_id")
                        if isinstance(entry, dict)
                        else f"fast-{sym}"
                    ),
                    "payload": {
                        "entry": dec,
                        "cache": entry,
                    },
                })
            except Exception as _push_exc:
                logger.debug("push_intent skipped (non-fatal): %s", _push_exc)

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