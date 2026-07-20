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


def _run_live_guard(config: dict, logger) -> dict | None:
    """Apply fast BE/trail on MT5 positions via position_manager."""
    if config.get("execution", {}).get("mode") != "mt5":
        return None
    from core.mt5_connection_manager import MT5ConnectionManager
    from core.position_manager import manage_mt5_positions
    from core.position_sync import fetch_mt5_agent_positions

    allowed = set(fast_mode_symbols(config))
    cache_syms = read_cache(config).get("symbols") or {}
    features_doc = read_json_state("features.json", default={"symbols": {}})
    connection = MT5ConnectionManager(config, logger)
    try:
        connection.connect()
        positions = fetch_mt5_agent_positions(config, logger)
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
    finally:
        try:
            connection.disconnect()
        except Exception:
            pass


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