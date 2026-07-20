"""Trade quantity limits — config helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.dynamic_entry import symbol_capacity_available as _symbol_capacity_available
from core.dynamic_entry import symbol_position_count
from core.utils import read_json_state


def unlimited_trades(config: dict[str, Any]) -> bool:
    """When true, no cap on candidates, exposure, or duplicate positions."""
    return bool(config.get("risk", {}).get("unlimited_trades", False))


def _max_open_confidence_from_positions(
    active_positions: list[dict[str, Any]] | None,
) -> float | None:
    """Strongest confidence among open positions, read from the broker's
    state/position_confidence.json sidecar (keyed by MT5 ticket). None when no
    open position has a recorded confidence."""
    if not active_positions:
        return None
    cmap = read_json_state("position_confidence.json", default={}) or {}
    best: float | None = None
    for p in active_positions:
        t = p.get("ticket")
        if t is None:
            continue
        c = cmap.get(str(t))
        if c is None:
            continue
        try:
            cf = float(c)
        except (TypeError, ValueError):
            continue
        if best is None or cf > best:
            best = cf
    return best


def allow_pyramiding(config: dict[str, Any]) -> bool:
    """When true, stack positions from different signals (same symbol/side allowed)."""
    return bool(config.get("trading", {}).get("allow_pyramiding", False))


def enrich_positions_with_orders(
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach signal_id/setup_type from filled orders onto live positions."""
    by_ticket: dict[int, dict[str, Any]] = {}
    for order in orders:
        ticket = order.get("mt5_ticket")
        if ticket is None or order.get("status") != "filled":
            continue
        by_ticket[int(ticket)] = order

    enriched: list[dict[str, Any]] = []
    for pos in positions:
        row = dict(pos)
        ticket = pos.get("ticket")
        if ticket is not None:
            order = by_ticket.get(int(ticket))
            if order:
                row.setdefault("signal_id", order.get("signal_id"))
                row.setdefault("setup_type", order.get("setup_type"))
        enriched.append(row)
    return enriched


def symbol_capacity_available(
    config: dict[str, Any],
    symbol: str,
    active_positions: list[dict[str, Any]],
) -> bool:
    """True when symbol is below max_open_per_symbol limit."""
    return _symbol_capacity_available(config, symbol, active_positions)


def is_duplicate_position(
    config: dict[str, Any],
    signal: dict[str, Any],
    active_positions: list[dict[str, Any]],
    *,
    executed_signal_ids: set[str] | None = None,
) -> bool:
    """
    Return True if this signal should not open another position.

    Pyramiding: allow same symbol/side when signal differs; block re-entry of
    the same signal_id. Optional pyramid_block_same_setup blocks identical setups.
    """
    sid = signal.get("signal_id")
    if sid and executed_signal_ids and sid in executed_signal_ids:
        return True

    symbol = signal.get("symbol")
    side = signal.get("side")
    setup = signal.get("setup_type")

    if not symbol_capacity_available(config, symbol, active_positions):
        return True

    from core.strategy_arena import arena_enabled, setup_capacity_available

    setup = signal.get("setup_type")
    if arena_enabled(config) and setup:
        if not setup_capacity_available(config, symbol, setup, active_positions):
            return True

    if allow_pyramiding(config):
        block_same_setup = bool(config.get("trading", {}).get("pyramid_block_same_setup", False))
        for active in active_positions:
            if sid and active.get("signal_id") == sid:
                return True
            if not block_same_setup:
                continue
            if (
                active.get("symbol") == symbol
                and active.get("side") == side
                and active.get("setup_type") == setup
            ):
                return True
        return False

    mode = config.get("execution", {}).get("mode", "paper")
    for active in active_positions:
        if active.get("symbol") != symbol or active.get("side") != side:
            continue
        if mode == "mt5":
            return True
        if active.get("setup_type") == setup:
            return True
    return False


def max_session_trades_per_symbol(config: dict[str, Any]) -> int | None:
    """Max closed trades allowed per symbol in the current session."""
    n = int(config.get("trading", {}).get("max_session_trades_per_symbol", 0))
    return n if n > 0 else None


def session_start_ts() -> datetime | None:
    """Session boundary from last equity/baseline reset (mt5_baseline.set_at)."""
    baseline = read_json_state("mt5_baseline.json", default={})
    raw = baseline.get("set_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def trades_in_current_session(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only closed trades at or after the current session start."""
    start = session_start_ts()
    if start is None:
        return trades
    kept: list[dict[str, Any]] = []
    for trade in trades:
        raw = trade.get("closed_at") or trade.get("timestamp")
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts >= start:
            kept.append(trade)
    return kept


def closed_trades_for_symbol(trades: list[dict[str, Any]], symbol: str) -> int:
    session_trades = trades_in_current_session(trades)
    return sum(1 for t in session_trades if t.get("symbol") == symbol)


def session_trade_capacity_available(
    config: dict[str, Any],
    symbol: str,
    closed_trades: list[dict[str, Any]],
) -> bool:
    """True when symbol is below max_session_trades_per_symbol limit."""
    limit = max_session_trades_per_symbol(config)
    if limit is None:
        return True
    return closed_trades_for_symbol(closed_trades, symbol) < limit


def reentry_cooldown_seconds(config: dict[str, Any]) -> float:
    trading = config.get("trading") or {}
    sec = trading.get("reentry_cooldown_seconds")
    if sec is not None:
        return max(0.0, float(sec))
    micro = (config.get("practice") or {}).get("micro") or {}
    if micro.get("enabled") and micro.get("reentry_cooldown_seconds") is not None:
        return max(0.0, float(micro["reentry_cooldown_seconds"]))
    return 0.0


def last_close_time_for_symbol(
    closed_trades: list[dict[str, Any]],
    symbol: str,
) -> datetime | None:
    latest: datetime | None = None
    for trade in closed_trades:
        if trade.get("symbol") != symbol:
            continue
        raw = trade.get("closed_at") or trade.get("timestamp")
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def symbol_reentry_available(
    config: dict[str, Any],
    symbol: str,
    closed_trades: list[dict[str, Any]],
) -> tuple[bool, float]:
    """True when enough time has passed since the last close on this symbol."""
    cooldown = reentry_cooldown_seconds(config)
    if cooldown <= 0:
        return True, 0.0
    last = last_close_time_for_symbol(closed_trades, symbol)
    if last is None:
        return True, 0.0
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    remaining = max(0.0, cooldown - elapsed)
    return elapsed >= cooldown, round(remaining, 1)


def session_trade_capacity_status(
    config: dict[str, Any],
    symbol: str,
    closed_trades: list[dict[str, Any]],
) -> dict[str, Any]:
    """Session closed-trade usage for dashboards and rejection messages."""
    limit = max_session_trades_per_symbol(config)
    used = closed_trades_for_symbol(closed_trades, symbol)
    if limit is None:
        return {"limit": None, "used": used, "available": True}
    return {
        "limit": limit,
        "used": used,
        "available": used < limit,
        "remaining": max(0, limit - used),
    }


def symbol_capacity_status(
    config: dict[str, Any],
    symbol: str,
    active_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Open-position usage for dashboards and rejection messages."""
    from core.dynamic_entry import max_open_per_symbol, symbol_position_count

    limit = max_open_per_symbol(config)
    used = symbol_position_count(active_positions, symbol)
    if limit is None:
        return {"limit": None, "used": used, "available": True}
    return {
        "limit": limit,
        "used": used,
        "available": used < limit,
        "remaining": max(0, limit - used),
    }


def humanize_verifier_failure(
    check_name: str,
    signal: dict[str, Any],
    config: dict[str, Any],
    *,
    active_positions: list[dict[str, Any]] | None = None,
    closed_trades: list[dict[str, Any]] | None = None,
) -> str:
    """Plain-English rejection reason for the dashboard."""
    symbol = signal.get("symbol", "?")
    active_positions = active_positions or []
    closed_trades = closed_trades or []

    open_status = symbol_capacity_status(config, symbol, active_positions)
    session_status = session_trade_capacity_status(config, symbol, closed_trades)

    if check_name == "symbol_capacity":
        return (
            f"Open position cap full — {symbol} already has "
            f"{open_status['used']}/{open_status['limit']} open (close it before a new entry)"
        )
    if check_name == "session_trade_capacity":
        return (
            f"Session trade cap full — {symbol} hit "
            f"{session_status['used']}/{session_status['limit']} closed trades this session "
            f"(run reset_equity_curve.py to start fresh)"
        )
    if check_name == "no_duplicate":
        return f"Duplicate blocked — same {symbol} {signal.get('side')} setup already open"
    if check_name == "reentry_cooldown":
        ok, remaining = symbol_reentry_available(config, symbol, closed_trades)
        if ok:
            return f"Re-entry cooldown — {symbol} ready"
        return (
            f"Re-entry cooldown — {symbol} closed recently; "
            f"wait {remaining:.0f}s more before a new entry"
        )
    if check_name == "entry_confirm":
        from core.entry_staging import touch_and_check
        status = touch_and_check(signal, config)
        if status.get("ready"):
            return f"Entry confirmed — {symbol} conditions held {status.get('age_sec', 0):.0f}s"
        need = status.get("need_sec", 0)
        age = status.get("age_sec", 0)
        return (
            f"Entry confirm pending — {symbol} {signal.get('side')} "
            f"needs {need:.0f}s of stable conditions ({age:.0f}s so far)"
        )
    if check_name == "max_loss_per_trade":
        from core.risk_cap import risk_per_trade_cap
        cap = risk_per_trade_cap(config)
        return (
            f"Max loss cap — {symbol} stop risk would exceed ${cap:.2f} at minimum lot"
            if cap is not None else "Max loss cap exceeded"
        )
    if check_name == "confidence_floor":
        open_conf = _max_open_confidence_from_positions(active_positions)
        sig_conf = signal.get("confidence")
        return (
            f"Confidence floor — new {symbol} {signal.get('side')} at {sig_conf}% is below "
            f"an open position at {open_conf}% (need stronger-or-equal confidence)"
        )
    if check_name == "exposure_limit_exceeded":
        cap = config.get("risk", {}).get("max_symbol_exposure_usd", 100)
        return f"Exposure limit — not enough room for {symbol} on ${cap} cap"
    if check_name == "blue_guardian_max_total":
        from core.blue_guardian import blue_guardian_settings

        max_total = blue_guardian_settings(config)["max_total_open_positions"]
        return (
            f"Blue Guardian cap — {open_status['used']}/{max_total} "
            f"total open positions (close one before a new entry)"
        )
    if check_name == "blue_guardian_symbol_full":
        return f"Blue Guardian cap — {symbol} already has an open position (max 1 per symbol)"
    if check_name == "blue_guardian_floating_block":
        return "Blue Guardian floating loss — combined unrealized worse than -$35, new entries blocked"
    if check_name == "blue_guardian_daily_pause":
        bg = read_json_state("blue_guardian.json", default={}) or {}
        return f"Blue Guardian daily pause — {bg.get('pause_reason') or 'limit hit'}"
    if check_name == "kill_switch_safe":
        return "Kill switch is ON — trading paused"
    if check_name == "risk_reward_safe":
        entry = float(signal.get("entry") or 0)
        sl = float(signal.get("sl") or 0)
        tp1 = float(signal.get("tp1") or 0)
        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        rr = (reward / risk) if risk > 0 else 0.0
        need = float(config.get("signals", {}).get("min_risk_reward", 1.2))
        return (
            f"Risk/reward too low — {rr:.2f}:1 (need {need:.1f}:1). "
            f"Confidence {signal.get('confidence', '?')}% is fine; widen TP or tighten SL."
        )
    if check_name == "regime_allowed":
        regime = (signal.get("market_context") or {}).get("market_regime") or {}
        return f"Regime skipped — {regime.get('primary', '?')} is untradeable under this strategy"
    if check_name == "regime_bias_aligned":
        regime = (signal.get("market_context") or {}).get("market_regime") or {}
        return (
            f"Regime bias mismatch — {regime.get('primary', '?')} is {regime.get('bias', '?')} "
            f"but signal is {signal.get('side', '?')}; only aligned trades taken in trend regimes"
        )
    if check_name == "win_condition_match":
        return (
            f"Win-condition gate — {signal.get('setup_type', '?')} @ "
            f"{(signal.get('market_context') or {}).get('market_regime', {}).get('primary', '?')} "
            f"did not match any condition cell that historically won for this setup; skipped"
        )
    if check_name == "data_driven_veto":
        mc = signal.get("market_context") or {}
        reg = mc.get("market_regime") or {}
        return (
            f"Data-driven veto — {signal.get('setup_type', '?')} on {symbol} @ "
            f"{reg.get('primary', '?')}/{mc.get('session', '?')} loses on clean live data; "
            f"pruned by the forward-test ledger"
        )
    if check_name == "positive_evolution":
        mc = signal.get("market_context") or {}
        reg = mc.get("market_regime") or {}
        return (
            f"Positive evolution gate — {signal.get('setup_type', '?')} on {symbol} @ "
            f"{reg.get('primary', '?')}/{mc.get('session', '?')} lacks proven positive expectancy; "
            f"cell blocked until culturing data improves"
        )
    return check_name.replace("_", " ")


def max_candidates_per_run(config: dict[str, Any]) -> int | None:
    """Return max candidates, or None for unlimited (0 or unlimited_trades)."""
    if unlimited_trades(config):
        return None
    n = int(config.get("signals", {}).get("max_candidates_per_run", 10))
    return None if n <= 0 else n