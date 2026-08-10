"""Phase 0 dashboard safety helpers."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, NoReturn


def _position_stop_risk(position: dict) -> float | None:
    """Return broker/planner stop risk only. Floating PnL is never risk."""
    for key in (
        "actual_stop_risk",
        "stop_risk",
        "risk_amount",
        "initial_risk_amount",
        "planned_risk",
    ):
        value = position.get(key)
        if value is None:
            continue
        try:
            risk = abs(float(value))
        except (TypeError, ValueError):
            continue
        if risk >= 0:
            return risk
    return None


def _freshness_timestamp(account: dict, portfolio: dict) -> tuple[str, str]:
    """Return the safest current timestamp and its source for the dashboard.

    The account/portfolio snapshots are intentionally slower than the market
    feed. Treating their timestamps as market freshness made an armed demo
    executor display DISARMED for minutes while quotes were current. Prefer the
    canonical market feed only when its worker is alive and error-free; if the
    feed is unhealthy or unavailable, fall back to the existing snapshots so
    the dashboard remains fail-closed.
    """
    try:
        from core.utils import read_json_state

        feed = read_json_state("market_data_feed.json", default={}) or {}
    except Exception:
        feed = {}

    feed_ok = (
        feed.get("worker_alive") is True
        and int(feed.get("refresh_failures_consecutive") or 0) == 0
    )
    market_ts = str(feed.get("last_market_timestamp") or "").strip()
    if feed_ok and market_ts:
        return market_ts, "market_data_feed"

    fallback = (
        portfolio.get("updated_at")
        or account.get("timestamp")
        or account.get("updated_at")
        or ""
    )
    return str(fallback or ""), "portfolio_or_account"


def build_safety_header(
    account: dict,
    config: dict | None = None,
    live_portfolio: dict | None = None,
    health: dict | None = None,
    kill_switch: dict | None = None,
    *,
    active_profile_name_fn=None,
) -> dict[str, Any]:
    """Build the fail-closed safety matrix displayed on every dashboard page."""
    cfg = config or {}
    execution = cfg.get("execution") or {}
    mt5_cfg = cfg.get("mt5") or {}
    learning_cfg = cfg.get("learning") or {}
    adaptation_cfg = cfg.get("adaptation") or {}
    portfolio = live_portfolio or {}
    positions = list(portfolio.get("open_positions") or [])

    profile = (cfg.get("profile") or {}).get("name")
    if not profile and active_profile_name_fn:
        try:
            profile = active_profile_name_fn()
        except Exception:
            profile = None
    profile = profile or "unknown"

    account_mode = str(
        account.get("account_mode")
        or mt5_cfg.get("account_mode")
        or "unknown"
    ).lower()

    data_updated, freshness_source = _freshness_timestamp(account, portfolio)
    data_age_s = -1.0
    if data_updated:
        try:
            parsed = datetime.fromisoformat(str(data_updated).replace("Z", "+00:00"))
            data_age_s = round(max(0.0, time.time() - parsed.timestamp()), 1)
        except Exception:
            data_age_s = -1.0

    if data_age_s < 0:
        freshness = "unknown"
    elif data_age_s > 30:
        freshness = "stale"
    elif data_age_s > 10:
        freshness = "aging"
    elif data_age_s > 2:
        freshness = "warm"
    else:
        freshness = "fresh"

    raw_health = str((health or {}).get("status") or "unknown").lower()
    health_status = {
        "healthy": "ok",
        "ok": "ok",
        "degraded": "degraded",
        "critical": "critical",
        "halted": "critical",
    }.get(raw_health, "unknown")

    try:
        from core.fast_mode import fast_mode_settings

        fast_cfg = fast_mode_settings(cfg)
    except Exception:
        fast_cfg = cfg.get("fast_mode") or {}

    risk_values = [_position_stop_risk(position) for position in positions]
    known_risks = [value for value in risk_values if value is not None]
    unknown_risk_positions = len(risk_values) - len(known_risks)
    open_risk = round(sum(known_risks), 2)

    kill_on = bool((kill_switch or {}).get("kill_switch", False))
    live_enabled = bool(execution.get("live_trading_enabled", False))
    explicit_opt_in = bool(execution.get("explicit_opt_in_danger_zone", False))
    fast_enabled = bool(fast_cfg.get("enabled", False))
    fast_live = bool(fast_cfg.get("live_enabled", False))
    adaptation_on = bool(adaptation_cfg.get("enabled", False))

    blockers: list[str] = []
    if kill_on:
        blockers.append("operator kill switch is on")
    if not live_enabled:
        blockers.append("live trading is disabled")
    if not explicit_opt_in:
        blockers.append("execution has no explicit opt-in")
    if health_status != "ok":
        blockers.append(f"health is {health_status}")
    if freshness not in {"fresh", "warm"}:
        blockers.append(f"data freshness is {freshness}")
    if account_mode == "real":
        blockers.append("real-account execution is blocked during Phase 0")
    if unknown_risk_positions:
        blockers.append(f"{unknown_risk_positions} position(s) have unknown stop risk")

    execution_allowed = not blockers

    return {
        "account_mode": account_mode,
        "account_login": portfolio.get("account_login") or account.get("login"),
        "account_server": portfolio.get("account_server") or account.get("server"),
        "profile": profile,
        "execution_allowed": execution_allowed,
        "execution_state": "ARMED" if execution_allowed else "DISARMED",
        "execution_blockers": blockers,
        "fast_mode": "LIVE" if fast_live else ("observe" if fast_enabled else "OFF"),
        "adaptation": "ON" if adaptation_on else "OFF",
        "learning": learning_cfg.get("mode", "observe_only"),
        "kill_switch": kill_on,
        "health": health_status,
        "open_positions": len(positions),
        "open_risk": open_risk,
        "open_risk_known": unknown_risk_positions == 0,
        "unknown_risk_positions": unknown_risk_positions,
        "data_source": portfolio.get("source"),
        "data_freshness": freshness,
        "data_age_seconds": data_age_s,
        "data_freshness_source": freshness_source,
        "data_updated_at": data_updated,
    }