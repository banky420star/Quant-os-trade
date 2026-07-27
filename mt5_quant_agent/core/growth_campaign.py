"""30-day growth campaign — track multi-day compounding run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.daily_growth import growth_plan_enabled, growth_settings, reset_daily_growth_baseline
from core.utils import read_json_state, utc_now_iso, write_json_state

STATE_FILE = "growth_campaign.json"


def campaign_enabled(config: dict[str, Any]) -> bool:
    if not growth_plan_enabled(config):
        return False
    days = int(growth_settings(config).get("campaign_days", 0))
    return days > 0


def start_campaign(equity: float, config: dict[str, Any]) -> dict[str, Any]:
    """Begin (or restart) a multi-day growth campaign from live equity."""
    growth = growth_settings(config)
    duration = int(growth.get("campaign_days", 30))
    daily_target = float(growth.get("daily_target_pct", 20))
    equity_f = round(float(equity), 2)
    start_day = datetime.now(timezone.utc).date()
    end_day = start_day + timedelta(days=duration - 1)
    daily_mult = 1.0 + daily_target / 100.0
    compound_target = round(equity_f * (daily_mult ** duration), 2)

    state = {
        "enabled": True,
        "active": True,
        "status": "running",
        "started_at": utc_now_iso(),
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "duration_days": duration,
        "daily_target_pct": daily_target,
        "start_equity": equity_f,
        "current_equity": equity_f,
        "campaign_pnl": 0.0,
        "campaign_pnl_pct": 0.0,
        "compound_target_equity": compound_target,
        "compound_target_pct": round((compound_target / equity_f - 1.0) * 100, 2) if equity_f else 0.0,
        "days_elapsed": 1,
        "days_remaining": duration - 1,
        "updated_at": utc_now_iso(),
    }
    write_json_state(STATE_FILE, state)
    reset_daily_growth_baseline(equity_f, config)
    return state


def update_campaign(equity: float, config: dict[str, Any]) -> dict[str, Any]:
    """Refresh campaign progress; mark complete when duration elapses."""
    if not campaign_enabled(config):
        return {"enabled": False}

    state = read_json_state(STATE_FILE, default={})
    if not state.get("active"):
        return {"enabled": True, "status": "not_started", "duration_days": int(growth_settings(config).get("campaign_days", 30))}

    growth = growth_settings(config)
    duration = int(state.get("duration_days") or growth.get("campaign_days", 30))
    daily_target = float(growth.get("daily_target_pct", state.get("daily_target_pct", 20)))
    start_day = datetime.fromisoformat(str(state["start_date"]) + "T00:00:00+00:00").date()
    today = datetime.now(timezone.utc).date()
    elapsed = max(1, (today - start_day).days + 1)
    remaining = max(0, duration - elapsed)

    start_eq = float(state.get("start_equity") or equity or 0)
    equity_f = round(float(equity), 2)
    pnl = round(equity_f - start_eq, 2)
    pnl_pct = round((pnl / start_eq) * 100, 2) if start_eq > 0 else 0.0
    daily_mult = 1.0 + daily_target / 100.0
    compound_target = round(start_eq * (daily_mult ** duration), 2)

    complete = elapsed >= duration
    status = "complete" if complete else "running"
    continuous = bool(growth.get("continuous_through_campaign", False))

    out = {
        **state,
        "enabled": True,
        "active": not complete,
        "status": status,
        "duration_days": duration,
        "daily_target_pct": daily_target,
        "current_equity": equity_f,
        "campaign_pnl": pnl,
        "campaign_pnl_pct": pnl_pct,
        "compound_target_equity": compound_target,
        "compound_target_pct": round((compound_target / start_eq - 1.0) * 100, 2) if start_eq else 0.0,
        "days_elapsed": min(elapsed, duration),
        "days_remaining": remaining,
        "continuous_mode": continuous,
        "updated_at": utc_now_iso(),
    }
    if complete and continuous:
        out["pause_reason"] = f"30-day campaign complete — {pnl_pct:+.2f}% total return"
    write_json_state(STATE_FILE, out)
    return out