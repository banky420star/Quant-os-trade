"""Fast entry executor — tick-reactive would-enter / would-cancel decisions."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.fast_mode import fast_mode_live, fast_mode_settings
from core.fast_signal_cache import read_fast_state
from core.microstructure import (
    anchor_distance_atr,
    price_in_zone,
    spread_ok,
    tick_momentum_score,
)
from core.utils import read_json_state, utc_now_iso


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _open_positions_for_symbol(symbol: str, config: dict[str, Any]) -> int:
    mode = str((config.get("execution") or {}).get("mode") or "paper").lower()
    filename = "mt5_positions.json" if mode == "mt5" else "paper_positions.json"
    pos_doc = read_json_state(filename, default={"positions": []})
    return sum(1 for p in pos_doc.get("positions", []) if p.get("symbol") == symbol)


def _trades_last_hour(symbol: str, state: dict[str, Any]) -> int:
    now = datetime.now(timezone.utc)
    hour_ago = now - timedelta(hours=1)
    bucket = list((state.get("trades_hour") or {}).get(symbol) or [])
    fresh = [t for t in bucket if (_parse_iso(t) or hour_ago) >= hour_ago]
    return len(fresh)


def _trades_last_10min(symbol: str, state: dict[str, Any]) -> int:
    now = datetime.now(timezone.utc)
    ten_ago = now - timedelta(minutes=10)
    bucket = list((state.get("trades_10min") or {}).get(symbol) or [])
    fresh = [t for t in bucket if (_parse_iso(t) or ten_ago) >= ten_ago]
    return len(fresh)



def _cooldown_active(symbol: str, config: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str]:
    cfg = fast_mode_settings(config)
    cooldown = int(cfg.get("cooldown_after_loss_seconds") or 0)
    if cooldown <= 0:
        return False, ""
    last = _parse_iso((state.get("last_loss_at") or {}).get(symbol))
    if not last:
        return False, ""
    if datetime.now(timezone.utc) < last + timedelta(seconds=cooldown):
        remain = int((last + timedelta(seconds=cooldown) - datetime.now(timezone.utc)).total_seconds())
        return True, f"loss_cooldown_{remain}s"
    return False, ""


def _rate_limits_ok(symbol: str, config: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str]:
    cfg = fast_mode_settings(config)
    max_hour = int(cfg.get("max_trades_per_symbol_per_hour") or 99)
    if _trades_last_hour(symbol, state) >= max_hour:
        return False, "max_trades_per_hour"
    max_10min = int(cfg.get("max_trades_per_10min") or 0)
    if max_10min and _trades_last_10min(symbol, state) >= max_10min:
        return False, f"max_trades_per_10min_{max_10min}"
    max_consec = int(cfg.get("max_consecutive_losses_per_symbol") or 99)
    consec = int((state.get("consecutive_losses") or {}).get(symbol) or 0)
    if max_consec and consec >= max_consec:
        return False, f"max_consecutive_losses_{consec}"
    cool, reason = _cooldown_active(symbol, config, state)
    if cool:
        return False, reason
    return True, "ok"


def evaluate_entry(
    cache_entry: dict[str, Any],
    *,
    mid: float,
    spread_points: float,
    feat: dict[str, Any],
    config: dict[str, Any],
    state: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Return a fast-mode decision dict (observe or live)."""
    log = logger or logging.getLogger("fast_entry_executor")
    cfg = fast_mode_settings(config)
    state = state if state is not None else read_fast_state()
    symbol = cache_entry.get("symbol", "")
    side = cache_entry.get("side", "")
    anchor = float(cache_entry.get("anchor") or 0)
    atr = float(feat.get("atr_14") or feat.get("atr") or max(anchor * 0.001, 0.01))
    zone_atr = float(cache_entry.get("trigger_zone_atr") or cfg.get("trigger_zone_atr") or 0.08)

    decision: dict[str, Any] = {
        "timestamp": utc_now_iso(),
        "symbol": symbol,
        "side": side,
        "signal_id": cache_entry.get("signal_id"),
        "mid": round(mid, 5),
        "anchor": anchor,
        "spread_points": round(spread_points, 2),
        "action": "wait",
        "live": fast_mode_live(config),
        "reasons": [],
    }

    # 2026-08-05 — dedupe FIRST (before the kill-switch / health / positions /
    # rate-limit gates which all do file reads every tick). The same cached
    # signal is re-evaluated every second, so once it is executed (or recently
    # refused for exposure) the loop must stop re-emitting "FAST LIVE entry"
    # and re-attempting the order — including all the wasted gate reads.
    # ``fast_tick_loop`` merges the mt5_orders.json ledger into
    # ``state.executed_signals`` once per tick, so slow-path-executed signals
    # are covered here too without a per-symbol file read.
    signal_id = str(cache_entry.get("signal_id") or "")
    if signal_id:
        if signal_id in set(state.get("executed_signals") or []):
            decision["action"] = "wait"
            decision["reasons"].append("already_executed")
            return decision
        _backoff_ts = (state.get("exposure_backoff") or {}).get(signal_id)
        _backoff_dt = _parse_iso(_backoff_ts)
        if _backoff_dt:
            _window = int(cfg.get("exposure_backoff_seconds") or 60)
            if datetime.now(timezone.utc) - _backoff_dt < timedelta(seconds=_window):
                decision["action"] = "wait"
                decision["reasons"].append("exposure_backoff")
                return decision

    kill = read_json_state("kill_switch.json", default={})
    if kill.get("kill_switch"):
        decision["action"] = "blocked"
        decision["reasons"].append(f"kill_switch:{kill.get('reason')}")
        return decision

    health = read_json_state("health.json", default={})
    if health.get("status") and health["status"] != "healthy":
        decision["action"] = "blocked"
        decision["reasons"].append("health_degraded")
        return decision

    max_open = int(cfg.get("max_open_positions") or 1)
    if _open_positions_for_symbol(symbol, config) >= max_open:
        decision["action"] = "blocked"
        decision["reasons"].append("max_open_positions")
        return decision

    ok_rate, rate_reason = _rate_limits_ok(symbol, config, state)
    if not ok_rate:
        decision["action"] = "blocked"
        decision["reasons"].append(rate_reason)
        return decision

    base_spread = float(feat.get("spread_points") or spread_points or 1)
    spread_mult = float(cfg.get("max_spread_mult") or 1.2)
    sp_ok, sp_reason = spread_ok(spread_points, base_spread, max_mult=spread_mult)
    if not sp_ok:
        decision["action"] = "blocked"
        decision["reasons"].append(sp_reason)
        return decision

    min_mom = float(cfg.get("min_tick_momentum_score") or 0)
    mom_score, _ = tick_momentum_score(feat, side)
    decision["momentum_score"] = round(mom_score, 1)
    if min_mom > 0 and mom_score < min_mom:
        decision["action"] = "wait"
        decision["reasons"].append(f"momentum_low_{mom_score:.0f}<{min_mom:.0f}")
        return decision

    dist_atr = anchor_distance_atr(mid, anchor, atr)
    decision["distance_atr"] = round(dist_atr, 4)
    max_anchor = float(cfg.get("max_anchor_distance_atr") or 0.35)
    if dist_atr > max_anchor:
        decision["action"] = "cancel"
        decision["reasons"].append(f"price_ran_away_{dist_atr:.2f}atr")
        return decision

    in_zone = price_in_zone(mid, anchor, atr, zone_atr=zone_atr)
    market_only = bool(
        (config.get("execution") or {}).get("strategy_entries_market_only", False)
        or (config.get("trading") or {}).get("strategy_entries_market_only", False)
    )
    entry_type = "market" if market_only else str(cache_entry.get("entry_type") or "limit")
    market_dist = float(cfg.get("market_if_distance_atr_below") or 0.05)

    would_market = (
        (market_only or cfg.get("allow_market_entries"))
        and dist_atr <= market_dist
        and (cfg.get("market_only_if_spread_ok", True) and sp_ok)
    )
    # Fixed data-lab runs have one authoritative market-order producer. Even if
    # a stale fast-mode preset says limit entries are allowed, never emit a
    # limit decision under the profile's market-only contract.
    would_limit = (
        not market_only
        and cfg.get("allow_limit_entries", True)
        and in_zone
    )

    if would_market:
        decision["action"] = "would_enter_market"
        decision["entry_type"] = "market"
        decision["reasons"].append("touch_zone_market")
    elif would_limit:
        decision["action"] = "would_enter_limit"
        decision["entry_type"] = "limit"
        decision["reasons"].append("in_entry_zone")
    else:
        decision["action"] = "wait"
        decision["reasons"].append("outside_zone")

    if decision["action"].startswith("would_enter") and fast_mode_live(config):
        decision["action"] = decision["action"].replace("would_enter", "enter")
        log.info(
            "FAST LIVE entry %s %s %s mid=%.5f anchor=%.5f",
            symbol,
            side,
            decision["entry_type"],
            mid,
            anchor,
        )
    elif decision["action"].startswith("would_enter"):
        log.debug(
            "FAST observe %s %s %s mid=%.5f dist=%.3fATR",
            symbol,
            side,
            decision.get("entry_type"),
            mid,
            dist_atr,
        )

    return decision