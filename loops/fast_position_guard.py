"""Fast position guard — quicker BE / trail / emergency exit decisions."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.audit_log import append_event
from core.fast_mode import fast_mode_enabled, fast_mode_live, fast_mode_settings, fast_mode_symbols
from core.fast_signal_cache import read_cache
from core.microstructure import anchor_distance_atr
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state

_LOGGER = None

# Throttled live-state sync so the dashboard reflects real MT5 activity within
# seconds instead of waiting for the slow 45s pipeline cycle. The fast scalper
# runs every ~1s and holds a live MT5 connection, so it is the ideal place to
# keep account/positions/trades state fresh between pipeline cycles.
import time as _time
import threading as _threading
_LIVE_SYNC_LAST = {"account": 0.0, "trades": 0.0}
_LIVE_SYNC_LOCK = _threading.Lock()
_LIVE_SYNC_ACCOUNT_EVERY = 4.0
# The closed-deals sync is heavier (history_deals_get + orders join), so run it
# less often to avoid adding latency to the 1s BE/trail guard tick.
_LIVE_SYNC_TRADES_EVERY = 30.0


def _logger():
    global _LOGGER
    if _LOGGER is None:
        _LOGGER = setup_logger("fast_position_guard", "fast_position_guard.log")
    return _LOGGER


def _risk_r(position: dict[str, Any], feat: dict[str, Any]) -> float:
    entry = float(position.get("entry") or position.get("entry_price") or 0)
    sl = float(position.get("sl") or position.get("stop_loss") or 0)
    profit = float(position.get("profit") or 0)
    if entry <= 0 or sl <= 0:
        return 0.0
    risk_dist = abs(entry - sl)
    if risk_dist <= 0:
        return 0.0
    side = str(position.get("side", "")).upper()
    point = float(feat.get("point") or 0.01)
    tick_value = float(feat.get("tick_value") or 1.0)
    # Approximate R from floating profit vs SL distance (micro lots).
    move = profit / max(tick_value * (risk_dist / point), 0.01)
    return move

def _feature_price(position: dict[str, Any], feat: dict[str, Any]) -> float | None:
    for key in ("current_price", "mid", "price", "close", "last_close"):
        val = position.get(key) if key in position else feat.get(key)
        if val is not None:
            price = float(val)
            if price > 0:
                return price
    side = str(position.get("side", "")).upper()
    if side == "BUY" and feat.get("bid") is not None:
        return float(feat["bid"])
    if side == "SELL" and feat.get("ask") is not None:
        return float(feat["ask"])
    return None


def _guard_management_config(config: dict[str, Any]) -> dict[str, Any]:
    cfg = fast_mode_settings(config)
    out = dict(config)
    trading = dict(out.get("trading") or {})
    fixed_exit_only = bool(
        (config.get("execution") or {}).get("fixed_exit_only", False)
        or (config.get("trading") or {}).get("fixed_exit_only", False)
    )
    if fixed_exit_only:
        # Never let this auxiliary guard mutate fixed-exit experiment positions.
        trading["break_even"] = {**(trading.get("break_even") or {}), "enabled": False}
        trading["trailing"] = {**(trading.get("trailing") or {}), "enabled": False}
        out["trading"] = trading
        return out
    be = dict(trading.get("break_even") or {})
    trail = dict(trading.get("trailing") or {})
    if (cfg.get("break_even_fast") or {}).get("enabled") is False:
        be["enabled"] = False
    if (cfg.get("trail_fast") or {}).get("enabled") is False:
        trail["enabled"] = False
    trading["break_even"] = be
    trading["trailing"] = trail
    out["trading"] = trading
    return out


def _format_live_guard_action(action: Any) -> dict[str, Any]:
    if isinstance(action, dict):
        row = dict(action)
        names = row.get("actions")
        if isinstance(names, list):
            row["action"] = ",".join(str(a) for a in names) or row.get("action") or "sl_update"
        else:
            row["action"] = str(row.get("action") or "sl_update")
        row["live"] = True
        row["timestamp"] = utc_now_iso()
        return row
    return {"action": str(action), "live": True, "timestamp": utc_now_iso()}


def _guard_actions(
    position: dict[str, Any],
    mgmt: dict[str, Any],
    feat: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    cfg = fast_mode_settings(config)
    actions: list[dict[str, Any]] = []
    fixed_exit_only = bool(
        (config.get("execution") or {}).get("fixed_exit_only", False)
        or (config.get("trading") or {}).get("fixed_exit_only", False)
    )
    if fixed_exit_only:
        return actions
    symbol = position.get("symbol", "")
    ticket = position.get("ticket") or position.get("position_id")
    r_mult = _risk_r(position, feat)
    be_cfg = cfg.get("break_even_fast") or {}
    trail_cfg = cfg.get("trail_fast") or {}
    be_trigger = float(mgmt.get("break_even_trigger_r") or be_cfg.get("trigger_r") or 0.25)
    trail_start = float(mgmt.get("trail_start_r") or trail_cfg.get("start_r") or 0.45)

    if be_cfg.get("enabled", True) and r_mult >= be_trigger:
        actions.append({
            "action": "would_move_be",
            "ticket": ticket,
            "symbol": symbol,
            "r_mult": round(r_mult, 3),
            "trigger_r": be_trigger,
        })
    if trail_cfg.get("enabled", True) and r_mult >= trail_start:
        actions.append({
            "action": "would_trail",
            "ticket": ticket,
            "symbol": symbol,
            "r_mult": round(r_mult, 3),
            "trail_start_r": trail_start,
        })

    emerg = cfg.get("emergency_exit") or {}
    if emerg.get("enabled"):
        adverse = float(emerg.get("adverse_tick_move_atr") or 0.25)
        entry = float(position.get("entry") or 0)
        profit = float(position.get("profit") or 0)
        atr = float(feat.get("atr_14") or feat.get("atr") or 0)
        mid = _feature_price(position, feat)
        if atr > 0 and profit < 0 and mid is not None:
            dist = anchor_distance_atr(mid, entry, atr)
            if dist >= adverse:
                actions.append({
                    "action": "would_emergency_exit",
                    "ticket": ticket,
                    "symbol": symbol,
                    "reason": "adverse_move",
                    "distance_atr": round(dist, 3),
                })
    return actions


def _sync_live_state(config: dict, connection, logger, positions: list | None = None) -> None:
    """Refresh account/positions/trades/equity-history state files so the
    dashboard and the new performance chart see live MT5 activity within a few
    seconds, even when the slow pipeline cycle is idle. Throttled; never raises.

    ``positions`` may be the positions already fetched by the caller this tick
    (avoids a duplicate positions_get call). The guard runs from the fast_mode
    service thread; _LIVE_SYNC_LOCK serializes the read-modify-write of the
    trades ledger against other writers."""
    now = _time.time()
    with _LIVE_SYNC_LOCK:
        try:
            if now - _LIVE_SYNC_LAST["account"] >= _LIVE_SYNC_ACCOUNT_EVERY:
                _LIVE_SYNC_LAST["account"] = now
                try:
                    snap = connection.account_snapshot()
                    if snap and snap.get("balance") is not None:
                        write_json_state("account.json", {
                            "timestamp": utc_now_iso(),
                            **snap,
                        })
                except Exception as exc:
                    logger.debug("live account sync skipped: %s", exc)
                try:
                    # Reuse the caller's positions when provided; fall back to a
                    # fresh fetch so the dashboard position tile stays live even
                    # when the guard path has no enriched positions.
                    if positions is None:
                        from core.position_sync import fetch_mt5_agent_positions
                        positions = fetch_mt5_agent_positions(config, logger)
                    write_json_state("mt5_positions.json", {
                        "timestamp": utc_now_iso(),
                        "mode": "mt5",
                        "positions": list(positions or []),
                    })
                except Exception as exc:
                    logger.debug("live positions sync skipped: %s", exc)
                try:
                    # Record an equity snapshot so the server-built equity_curve
                    # (the chart's baseline on the heavy poll) is also fresh —
                    # otherwise the fast /api/live appends get clobbered by stale
                    # setData() every heavy poll.
                    from core.equity_tracker import record_snapshot
                    eq = float(snap.get("equity") or 0) if snap else 0.0
                    bal = float(snap.get("balance") or 0) if snap else 0.0
                    if eq > 0:
                        record_snapshot(eq, bal, source="fast_guard")
                except Exception as exc:
                    logger.debug("live equity-history sync skipped: %s", exc)
            if now - _LIVE_SYNC_LAST["trades"] >= _LIVE_SYNC_TRADES_EVERY:
                _LIVE_SYNC_LAST["trades"] = now
                try:
                    from core.trade_tracker import TradeTracker
                    existing = read_json_state("mt5_trades.json", default={"trades": []})
                    magic = int(config.get("execution", {}).get("magic_number", 20250625))
                    merged, _added = TradeTracker(logger).sync_mt5_closed_deals(
                        list(existing.get("trades") or []), magic, days=7,
                    )
                    write_json_state("mt5_trades.json", {
                        "timestamp": utc_now_iso(),
                        "mode": "mt5",
                        "trades": merged,
                    })
                except Exception as exc:
                    logger.debug("live trades sync skipped: %s", exc)
        except Exception as exc:
            logger.debug("live state sync failed: %s", exc)


# Cached shared MT5 connection for the live guard path. The guard runs on
# every ~1s fast tick; creating + disconnecting an MT5ConnectionManager per
# tick was the 2026-08-04 ownership bug — its disconnect() called
# mt5.shutdown() (process-global) and unplugged the persistent session that
# fast_tick_loop was holding. The guard now reuses ONE cached connection via
# the shared MT5Owner and NEVER disconnects it.
_GUARD_CONN = None
_GUARD_CONN_LOCK = _threading.Lock()


def _get_guard_connection(config: dict, logger) -> Any:
    """Return the cached live-guard MT5 connection, reconnecting if dead.

    Mirrors fast_tick_loop's persistent-connection pattern so the guard no
    longer opens/closes an MT5 session every tick."""
    global _GUARD_CONN
    from core.mt5_connection_manager import MT5ConnectionManager

    with _GUARD_CONN_LOCK:
        if _GUARD_CONN is not None and _GUARD_CONN.connected:
            try:
                ping = _GUARD_CONN.ping()
                if ping.get("alive") and ping.get("logged_in"):
                    return _GUARD_CONN
            except Exception:
                pass
            # Session died — the OWNER re-establishes it (owner-owned shutdown,
            # not a per-worker disconnect).
            try:
                if _GUARD_CONN.reconnect():
                    return _GUARD_CONN
            except Exception:
                pass
            _GUARD_CONN = None
        if _GUARD_CONN is None:
            conn = MT5ConnectionManager(config, logger)
            conn.connect()
            _GUARD_CONN = conn
        return _GUARD_CONN


def _run_live_guard(config: dict, logger) -> dict | None:
    """Apply fast BE/trail on MT5 positions via position_manager."""
    if config.get("execution", {}).get("mode") != "mt5":
        return None
    from core.position_manager import manage_mt5_positions
    from core.position_sync import fetch_mt5_agent_positions

    allowed = set(fast_mode_symbols(config))
    cache_syms = read_cache(config).get("symbols") or {}
    features_doc = read_json_state("features.json", default={"symbols": {}})
    connection = _get_guard_connection(config, logger)
    positions = fetch_mt5_agent_positions(config, logger)
    # Fast-path live-state sync: dashboard trades tracker + chart stay fresh
    # without waiting for the slow pipeline cycle. Reuse the positions we
    # already fetched this tick.
    _sync_live_state(config, connection, logger, positions=positions)
    enriched: list[dict[str, Any]] = []
    for pos in positions:
        sym = pos.get("symbol", "")
        if allowed and sym not in allowed:
            continue
        row = dict(pos)
        mgmt = (cache_syms.get(sym) or {}).get("management_profile")
        if not mgmt:
            cfg = fast_mode_settings(config)
            mgmt = {
                "break_even_trigger_r": float((cfg.get("break_even_fast") or {}).get("trigger_r") or 0.25),
                "trail_start_r": float((cfg.get("trail_fast") or {}).get("start_r") or 0.45),
                "trail_atr_mult": float((cfg.get("trail_fast") or {}).get("atr_mult") or 0.35),
            }
        row["management_profile"] = mgmt
        enriched.append(row)
    if not enriched:
        return {
            "timestamp": utc_now_iso(),
            "mode": "live",
            "position_count": 0,
            "actions": [],
        }
    guard_config = _guard_management_config(config)
    summary = manage_mt5_positions(guard_config, enriched, features_doc, logger)
    actions = [_format_live_guard_action(a) for a in list(summary.get("actions") or [])]
    for act in actions:
        append_event("fast_mode.guard", symbol=act.get("symbol"), details=act)
    return {
        "timestamp": summary.get("timestamp") or utc_now_iso(),
        "mode": "live",
        "position_count": len(enriched),
        "actions": actions,
        "updated": summary.get("updated", 0),
    }


def run(config: dict | None = None) -> dict | None:
    if config is None:
        config = load_config()
    logger = _logger()

    if not fast_mode_enabled(config):
        return None

    if fast_mode_live(config) and config.get("execution", {}).get("mode") == "mt5":
        doc = _run_live_guard(config, logger)
        if doc is not None:
            write_json_state("fast_mode_guard.json", doc)
            from core.state_store import get_state_store, state_store_enabled
            store = get_state_store(config)
            if store and state_store_enabled(config):
                store.set_kv("fast_mode_guard", doc)
            if doc.get("actions"):
                logger.info(
                    "Fast guard LIVE: %d SL updates on %d positions",
                    doc.get("updated", 0),
                    doc.get("position_count", 0),
                )
            return doc

    positions = list(read_json_state("paper_positions.json", default={"positions": []}).get("positions") or [])
    cache = read_cache(config)
    cache_syms = cache.get("symbols") or {}
    features = read_json_state("features.json", default={"symbols": {}})
    all_actions: list[dict[str, Any]] = []

    for pos in positions:
        sym = pos.get("symbol", "")
        mgmt = (cache_syms.get(sym) or {}).get("management_profile") or {}
        if not mgmt:
            cfg = fast_mode_settings(config)
            mgmt = {
                "break_even_trigger_r": float((cfg.get("break_even_fast") or {}).get("trigger_r") or 0.25),
                "trail_start_r": float((cfg.get("trail_fast") or {}).get("start_r") or 0.45),
            }
        feat = (features.get("symbols") or {}).get(sym) or {}
        actions = _guard_actions(pos, mgmt, feat, config)
        for act in actions:
            act["timestamp"] = utc_now_iso()
            act["live"] = fast_mode_live(config)
            if fast_mode_live(config) and act["action"].startswith("would_"):
                act["action"] = act["action"].replace("would_", "")
                append_event("fast_mode.guard", symbol=sym, details=act)
            all_actions.append(act)

    doc = {
        "timestamp": utc_now_iso(),
        "mode": "live" if fast_mode_live(config) else "observe",
        "position_count": len(positions),
        "actions": all_actions,
    }
    write_json_state("fast_mode_guard.json", doc)
    store = None
    from core.state_store import get_state_store, state_store_enabled
    store = get_state_store(config)
    if store and state_store_enabled(config):
        store.set_kv("fast_mode_guard", doc)

    if all_actions:
        logger.info("Fast guard: %d actions on %d positions", len(all_actions), len(positions))
    return doc


if __name__ == "__main__":
    run()