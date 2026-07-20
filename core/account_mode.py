"""Account mode helpers — practice vs live performance plan."""

from __future__ import annotations

from typing import Any

from core.blue_guardian import blue_guardian_enabled, blue_guardian_settings
from core.daily_growth import growth_plan_enabled, growth_settings
from core.micro_profile import micro_profile_enabled, micro_settings
from core.performance_projection import TARGET_MONTHLY_PNL_USD
from core.strategy_arena import arena_enabled, arena_settings
from core.utils import read_json_state


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
    if blue_guardian_enabled(config) and account_mode == "real":
        bg = blue_guardian_settings(config)
        label = "blue_guardian"
        detail = (
            f"Blue Guardian Instant ${bg['account_size_usd']:,.0f} eval on {account_mode} account "
            f"(target +{bg['daily_profit_target_pct']:.0f}%/day, ${bg['daily_profit_target_usd']:.0f})"
        )
    elif plan_active:
        label = "live_plan"
        detail = (
            f"$50k performance plan active on {account_mode} account "
            f"(target ${perf.get('target_monthly_pnl_usd', TARGET_MONTHLY_PNL_USD):,.0f}/mo)"
        )
    elif micro_profile_enabled(config) and micro_settings(config).get("live_mode"):
        micro = micro_settings(config)
        ref = float(micro.get("account_size_usd", 30))
        sym_s = ", ".join(micro.get("symbols") or [])
        label = "micro_live"
        detail = (
            f"${ref:.0f} micro LIVE on {account_mode} — {sym_s} · "
            f"0.01 lot · ${float(micro.get('max_loss_per_trade_usd', 10)):.0f} max loss/trade"
        )
    else:
        label = "practice"
        apply_when = perf.get("apply_when", "real")
        if growth_plan_enabled(config):
            growth = growth_settings(config)
            target = float(growth.get("daily_target_pct", 20))
            label = "growth"
            days = int(growth.get("campaign_days", 0))
            if arena_enabled(config):
                ar = arena_settings(config)
                sym_s = ", ".join(ar.get("symbols") or [])
                kelly = float(
                    (config.get("signals", {}) or {}).get("kelly_sizing", {}).get("kelly_fraction", 0.25)
                )
                k_label = "full-Kelly" if kelly >= 0.99 else f"{kelly:.0%}-Kelly"
                if micro_profile_enabled(config):
                    micro = micro_settings(config)
                    ref = float(micro.get("account_size_usd", 30))
                    label = "micro_growth"
                    detail = (
                        f"${ref:.0f} micro growth on {account_mode} — {sym_s} · "
                        f"no pyramiding · 0.01 lot cap · +{target:.0f}%/day target"
                    )
                else:
                    detail = (
                        f"Full-tilt arena on {account_mode} — {sym_s} · all setups compete · "
                        f"{k_label} · +{target:.0f}%/day target"
                    )
            elif days > 0:
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
    baseline = read_json_state("mt5_baseline.json", default={}) or {}
    account = read_json_state("account.json", default={}) or {}
    starting_cash = baseline.get("starting_cash")
    if starting_cash is None:
        starting_cash = account.get("balance") or config.get("execution", {}).get("starting_cash", 0)
    return {
        "label": label,
        "detail": detail,
        "account_mode": account_mode,
        "performance_plan_active": plan_active,
        "growth_plan_active": growth_plan_enabled(config),
        "micro_profile_active": micro_profile_enabled(config),
        "arena_active": arena_enabled(config),
        "daily_target_pct": float(growth.get("daily_target_pct", 0)) if growth else 0.0,
        "apply_when": perf.get("apply_when", "real"),
        "starting_cash": float(starting_cash or 0),
    }


def validate_runtime_profile(config: dict[str, Any]) -> dict[str, Any]:
    """Refuse startup when the live account and active profile disagree."""
    account_mode = str(config.get("mt5", {}).get("account_mode", "demo")).lower()
    perf_active = performance_gates_active(config)
    growth_active = growth_plan_enabled(config)
    issues: list[str] = []

    bg_eval = blue_guardian_enabled(config) and account_mode == "real"

    micro_live = micro_profile_enabled(config) and bool(micro_settings(config).get("live_mode"))

    if account_mode == "real":
        if not perf_active and not bg_eval and not micro_live:
            issues.append("real_account_requires_performance.apply_when=real")
        if growth_active and not bg_eval:
            issues.append("real_account_cannot_run_practice_growth")
    elif account_mode == "demo" and perf_active and growth_active:
        issues.append("demo_account_conflicts_with_growth_campaign")

    if issues:
        raise RuntimeError("Runtime profile mismatch: " + "; ".join(issues))

    return {
        "account_mode": account_mode,
        "performance_plan_active": perf_active,
        "growth_plan_active": growth_active,
    }
