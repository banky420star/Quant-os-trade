"""Fast execution cache — slow brain writes, fast hands read."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.fast_mode import fast_mode_settings, fast_mode_symbols
from core.microstructure import entry_zone_bounds
from core.state_store import get_state_store, state_store_enabled
from core.utils import read_json_state, utc_now_iso, write_json_state

CACHE_FILE = "fast_signal_cache.json"
STATE_FILE = "fast_mode_state.json"


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _expire_seconds(config: dict[str, Any], signal: dict[str, Any]) -> int:
    cfg = fast_mode_settings(config)
    mgmt = signal.get("management_profile") or {}
    return int(
        mgmt.get("cancel_if_not_filled_seconds")
        or cfg.get("cancel_stale_seconds")
        or 60
    )


def _build_cache_entry(signal: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    symbol = signal.get("symbol")
    if not symbol:
        return None
    if signal.get("evaluation", {}).get("action") == "skip":
        return None
    exec_pol = signal.get("execution_policy") or {}
    if exec_pol.get("action") == "skip":
        return None

    entry = float(signal.get("entry") or signal.get("anchor") or 0)
    if entry <= 0:
        return None

    feat = {}
    features = read_json_state("features.json", default={"symbols": {}})
    feat = (features.get("symbols") or {}).get(symbol) or {}
    atr = float(feat.get("atr_14") or feat.get("atr") or 0)
    if atr <= 0:
        atr = max(abs(entry) * 0.001, 0.01)

    cfg = fast_mode_settings(config)
    zone_atr = float(cfg.get("trigger_zone_atr") or 0.08)
    lo, hi = entry_zone_bounds(entry, atr, zone_atr)
    ttl = _expire_seconds(config, signal)
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=ttl)).isoformat()

    mgmt = dict(signal.get("management_profile") or {})
    be_fast = cfg.get("break_even_fast") or {}
    trail_fast = cfg.get("trail_fast") or {}
    be_per_sym = (be_fast.get("per_symbol") or {}).get(symbol) or {}
    if be_fast.get("enabled"):
        default_trigger = float(be_fast.get("trigger_r", mgmt.get("break_even_trigger_r", 0.4)))
        default_lock = float(be_fast.get("lock_r", mgmt.get("break_even_lock_r", 0.05)))
        mgmt["break_even_trigger_r"] = float(be_per_sym.get("trigger_r", default_trigger))
        mgmt["break_even_lock_r"] = float(be_per_sym.get("lock_r", default_lock))
    if trail_fast.get("enabled"):
        mgmt["trail_start_r"] = float(trail_fast.get("start_r", mgmt.get("trail_start_r", 0.65)))
        mgmt["trail_atr_mult"] = float(trail_fast.get("atr_mult", mgmt.get("trail_atr_mult", 0.43)))

    return {
        "signal_id": signal.get("signal_id"),
        "symbol": symbol,
        "side": signal.get("side"),
        "setup_type": signal.get("setup_type"),
        "allowed": True,
        "anchor": round(entry, 5),
        "entry_zone": [lo, hi],
        "entry_type": exec_pol.get("entry_type") or mgmt.get("entry_type") or "limit",
        "policy_score": (signal.get("evaluation") or {}).get("policy_score"),
        "expires_at": expires,
        "management_profile": mgmt,
        "trigger_zone_atr": zone_atr,
        "max_spread_points": float(cfg.get("max_spread_points") or feat.get("spread_points") or 0) or None,
        "micro_momentum_required": bool(cfg.get("min_tick_momentum_score", 0) > 0),
        "cached_at": utc_now_iso(),
    }


def refresh_cache(
    config: dict[str, Any],
    *,
    evaluated_doc: dict[str, Any] | None = None,
    approved_doc: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Rebuild fast_signal_cache from evaluated (+ optional approved) signals."""
    log = logger or logging.getLogger("fast_signal_cache")
    allowed_syms = set(fast_mode_symbols(config))
    entries: dict[str, Any] = {}

    ev_doc = evaluated_doc if evaluated_doc is not None else read_json_state("evaluated_signals.json", default={})
    for signal in list(ev_doc.get("evaluated") or []):
        sym = signal.get("symbol")
        if allowed_syms and sym not in allowed_syms:
            continue
        row = _build_cache_entry(signal, config)
        if row:
            entries[sym] = row

    cfg = fast_mode_settings(config)
    if not cfg.get("require_evaluated_signal", True) and approved_doc is not None:
        for record in list(approved_doc.get("approved") or []):
            signal = record.get("signal", record) if isinstance(record, dict) else {}
            sym = signal.get("symbol")
            if sym in entries or (allowed_syms and sym not in allowed_syms):
                continue
            row = _build_cache_entry(signal, config)
            if row:
                entries[sym] = row

    doc = {
        "timestamp": utc_now_iso(),
        "mode": "live" if cfg.get("live_enabled") else "observe",
        "symbol_count": len(entries),
        "symbols": entries,
    }
    write_json_state(CACHE_FILE, doc)
    store = get_state_store(config)
    if store and state_store_enabled(config):
        store.set_kv("fast_signal_cache", doc)
    log.info("Fast cache refreshed: %d symbols", len(entries))
    return doc


def read_cache(config: dict[str, Any] | None = None) -> dict[str, Any]:
    if config:
        from core.state_store import read_from_sqlite
        store = get_state_store(config)
        if store and read_from_sqlite(config):
            kv = store.get_kv("fast_signal_cache")
            if isinstance(kv, dict) and kv.get("symbols") is not None:
                return kv
    return read_json_state(CACHE_FILE, default={"symbols": {}})


def read_fast_state() -> dict[str, Any]:
    return read_json_state(STATE_FILE, default={
        "trades_hour": {},
        "trades_10min": {},
        "consecutive_losses": {},
        "last_loss_at": {},
        "pending_actions": {},
        "executed_signals": [],
        "exposure_backoff": {},
    })


def write_fast_state(state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now_iso()
    write_json_state(STATE_FILE, state)


def record_trade_entry(symbol: str, state: dict[str, Any]) -> None:
    """Record a fast entry timestamp for both 1h and 10m rate-limit windows."""
    now = utc_now_iso()
    hour_bucket = dict(state.get("trades_hour") or {})
    hour_times = list(hour_bucket.get(symbol) or [])
    hour_times.append(now)
    hour_cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    hour_times = [t for t in hour_times if (_parse_iso(t) or hour_cutoff) >= hour_cutoff]
    hour_bucket[symbol] = hour_times[-20:]
    state["trades_hour"] = hour_bucket

    ten_bucket = dict(state.get("trades_10min") or {})
    ten_times = list(ten_bucket.get(symbol) or [])
    ten_times.append(now)
    ten_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    ten_times = [t for t in ten_times if (_parse_iso(t) or ten_cutoff) >= ten_cutoff]
    ten_bucket[symbol] = ten_times[-20:]
    state["trades_10min"] = ten_bucket
    write_fast_state(state)


def prune_expired(cache: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    symbols = dict(cache.get("symbols") or {})
    kept: dict[str, Any] = {}
    for sym, row in symbols.items():
        exp = _parse_iso(row.get("expires_at"))
        if exp and exp < now:
            continue
        kept[sym] = row
    return {**cache, "symbols": kept, "symbol_count": len(kept)}