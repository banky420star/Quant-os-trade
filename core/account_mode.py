"""Account mode helpers — practice vs live performance plan."""

from __future__ import annotations

from typing import Any

from core.daily_growth import growth_plan_enabled, growth_settings
from core.performance_projection import TARGET_MONTHLY_PNL_USD


def performance_gates_active(config: dict[str, Any]) -> bool:
    """Whether $50k-plan gates should apply to the live pipeline."""
    perf = config.get("performance") or {}
    apply_when = str(perf.get("apply_when", "real")).lower()
    if apply_when in ("never", "off", "false"):
        return False
    if apply_when in ("always", "on", "true"):
        return True
    account_mode = str(config.get("mt5", {}).get("account_mode", "demo")).lower()
    return account_mode == apply_when


def runtime_mode_summary(config: dict[str, Any]) -> dict[str, Any]:
    """Human-readable practice vs live-plan status for logs and dashboard."""
    account_mode = str(config.get("mt5", {}).get("account_mode", "demo")).lower()
    plan_active = performance_gates_active(config)
    perf = config.get("performance") or {}
    if plan_active:
        label = "live_plan"
        detail = (
            f"$50k performance plan active on {account_mode} account "
            f"(target ${perf.get('target_monthly_pnl_usd', TARGET_MONTHLY_PNL_USD):,.0f}/mo)"
        )
    else:
        label = "practice"
        apply_when = perf.get("apply_when", "real")
        if growth_plan_enabled(config):
            growth = growth_settings(config)
            target = float(growth.get("daily_target_pct", 20))
            label = "growth"
            days = int(growth.get("campaign_days", 0))
            if days > 0:
                detail = (
                    f"30-day growth run on {account_mode} — +{target:.0f}%/day for {days} days "
                    f"(demo practice; not guaranteed)"
                )
            else:
                detail = (
                    f"20% daily growth plan on {account_mode} — target +{target:.0f}% balance per day "
                    f"(demo practice; not guaranteed)"
                )
        else:
            detail = (
                f"Practice mode on {account_mode} account — "
                f"set mt5.account_mode: {apply_when} to activate the performance plan"
            )
    growth = growth_settings(config) if growth_plan_enabled(config) else {}
    return {
        "label": label,
        "detail": detail,
        "account_mode": account_mode,
        "performance_plan_active": plan_active,
        "growth_plan_active": growth_plan_enabled(config),
        "daily_target_pct": float(growth.get("daily_target_pct", 0)) if growth else 0.0,
        "apply_when": perf.get("apply_when", "real"),
        "starting_cash": float(config.get("execution", {}).get("starting_cash", 0)),
    }