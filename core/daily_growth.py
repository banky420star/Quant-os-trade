"""Daily growth target — track % balance gain per day and pause at goal."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.utils import read_json_state, utc_now_iso, write_json_state

STATE_FILE = "daily_growth.json"


def _performance_gates_active(config: dict[str, Any]) -> bool:
    perf = config.get("performance") or {}
    apply_when = str(perf.get("apply_when", "real")).lower()
    if apply_when in ("never", "off", "false"):
        return False
    if apply_when in ("always", "on", "true"):
        return True
    account_mode = str(config.get("mt5", {}).get("account_mode", "demo")).lower()
    return account_mode == apply_when


def growth_plan_enabled(config: dict[str, Any]) -> bool:
    if _performance_gates_active(config):
        return False
    practice = config.get("practice") or {}
    growth = practice.get("growth") or {}
    return bool(growth.get("enabled", False))


def growth_settings(config: dict[str, Any]) -> dict[str, Any]:
    practice = config.get("practice") or {}
    return dict(practice.get("growth") or {})


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def reset_daily_growth_baseline(equity: float, config: dict[str, Any]) -> dict[str, Any]:
    """Force today's baseline to live equity (practice restart / manual reset)."""
    if not growth_plan_enabled(config):
        return {"enabled": False}
    growth = growth_settings(config)
    target_pct = float(growth.get("daily_target_pct", 20))
    equity_f = round(float(equity), 2)
    state = {
        "enabled": True,
        "day": _utc_day(),
        "day_start_equity": equity_f,
        "current_equity": equity_f,
        "daily_pnl": 0.0,
        "daily_pnl_pct": 0.0,
        "target_pct": target_pct,
        "remaining_pct": target_pct,
        "target_hit": False,
        "max_daily_loss_pct": float(growth.get("max_daily_loss_pct", 12)),
        "trading_paused": False,
        "pause_reason": None,
        "updated_at": utc_now_iso(),
    }
    write_json_state(STATE_FILE, state)
    return state


def sync_daily_session(equity: float, config: dict[str, Any]) -> dict[str, Any]:
    """Roll day baseline at UTC midnight; persist progress toward daily target."""
    growth = growth_settings(config)
    target_pct = float(growth.get("daily_target_pct", 20))
    today = _utc_day()
    state = read_json_state(STATE_FILE, default={})
    equity_f = round(float(equity), 2)

    if state.get("day") != today or not state.get("day_start_equity"):
        state = {
            "day": today,
            "day_start_equity": equity_f,
            "target_pct": target_pct,
            "target_hit": False,
            "trading_paused": False,
            "pause_reason": None,
            "updated_at": utc_now_iso(),
        }
        write_json_state(STATE_FILE, state)
        return state

    state["target_pct"] = target_pct
    state["updated_at"] = utc_now_iso()
    write_json_state(STATE_FILE, state)
    return state


def evaluate_daily_growth(
    equity: float,
    config: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute daily PnL % and whether to pause trading (target hit or max daily loss).

    USER-AUTHORIZED 2026-06-30 fix: the max-daily-LOSS pause is a SAFETY backstop
    that must run in EVERY mode (conservative OR growth). Previously this returned
    {"enabled": False} when growth_plan was disabled (apply_when matched
    account_mode), which made risk_manager skip the daily-loss pause entirely —
    so the bot stacked positions into a 14:30 UTC news spike with no daily-loss
    circuit-breaker and crashed -57.6%. Now: day-tracking + max-loss pause ALWAYS
    run; the +target/lock logic runs only when growth_plan is enabled. `enabled`
    reflects growth-plan status (so risk_manager's drawdown-base selection is
    unchanged in conservative mode), but `trading_paused`/`pause_reason` reflect
    the max-loss pause regardless of mode.
    """
    growth = growth_settings(config)
    plan_on = growth_plan_enabled(config)
    state = dict(existing or sync_daily_session(equity, config))
    start = float(state.get("day_start_equity") or equity or 0)
    equity_f = float(equity)
    target_pct = float(state.get("target_pct") or growth.get("daily_target_pct", 20))
    max_loss_pct = float(growth.get("max_daily_loss_pct", 12))
    lock_on_target = bool(growth.get("lock_profit_when_target_hit", True))
    continuous = bool(growth.get("continuous_through_campaign", False))
    campaign_days = int(growth.get("campaign_days", 0))
    if continuous and campaign_days > 0:
        lock_on_target = False

    pnl = round(equity_f - start, 2)
    pnl_pct = round((pnl / start) * 100, 2) if start > 0 else 0.0
    remaining_pct = round(max(0.0, target_pct - pnl_pct), 2)
    target_hit = pnl_pct >= target_pct if plan_on else False
    max_loss_hit = pnl_pct <= -max_loss_pct

    trading_paused = False
    pause_reason = None

    if plan_on and target_hit and lock_on_target:
        trading_paused = True
        pause_reason = f"Daily target +{target_pct:.0f}% reached ({pnl_pct:+.2f}%) — locked until tomorrow"
    elif plan_on and target_hit and continuous:
        trading_paused = False
        pause_reason = None
    elif max_loss_hit:
        trading_paused = True
        pause_reason = f"Max daily loss -{max_loss_pct:.0f}% hit ({pnl_pct:+.2f}%) — paused until tomorrow"

    out = {
        "enabled": plan_on,
        "day": state.get("day", _utc_day()),
        "day_start_equity": round(start, 2),
        "current_equity": round(equity_f, 2),
        "daily_pnl": pnl,
        "daily_pnl_pct": pnl_pct,
        "target_pct": target_pct,
        "remaining_pct": remaining_pct,
        "target_hit": target_hit,
        "max_daily_loss_pct": max_loss_pct,
        "trading_paused": trading_paused,
        "pause_reason": pause_reason,
        "updated_at": utc_now_iso(),
    }
    write_json_state(STATE_FILE, out)
    return out