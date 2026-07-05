"""Growth-awareness for historical replay — track daily target vs $30 campaign."""

from __future__ import annotations

from typing import Any

from core.daily_growth import growth_plan_enabled, growth_settings
from core.growth_campaign import campaign_enabled


def growth_replay_settings(config: dict[str, Any]) -> dict[str, Any]:
    micro = (config.get("practice") or {}).get("micro") or {}
    growth = growth_settings(config)
    enabled = growth_plan_enabled(config) or bool(micro.get("enabled"))
    daily_target = float(micro.get("daily_target_pct") or growth.get("daily_target_pct", 20))
    max_loss = float(micro.get("max_daily_loss_pct") or growth.get("max_daily_loss_pct", 10))
    campaign_days = int(growth.get("campaign_days", 30))
    start_equity = float(
        micro.get("account_size_usd")
        or config.get("execution", {}).get("starting_cash", 30)
    )
    return {
        "enabled": enabled,
        "daily_target_pct": daily_target,
        "max_daily_loss_pct": max_loss,
        "campaign_days": campaign_days,
        "start_equity": start_equity,
        "lock_on_target": bool(growth.get("lock_profit_when_target_hit", True)),
        "campaign_enabled": campaign_enabled(config),
    }


class GrowthReplayTracker:
    """Simulate daily growth targets bar-by-bar during replay."""

    def __init__(self, config: dict[str, Any], starting_equity: float | None = None):
        self.cfg = growth_replay_settings(config)
        self.start_equity = float(starting_equity or self.cfg["start_equity"])
        self.current_day: str | None = None
        self.day_start_equity = self.start_equity
        self.campaign_start_equity = self.start_equity
        self.trading_paused = False
        self.pause_reason: str | None = None
        self.target_hit_days = 0
        self.loss_pause_days = 0
        self.days_tracked = 0
        self.peak_equity = self.start_equity
        self.daily_log: list[dict[str, Any]] = []

    def _utc_day(self, bar_time: Any) -> str:
        try:
            import pandas as pd
            return pd.Timestamp(bar_time).strftime("%Y-%m-%d")
        except Exception:
            from datetime import datetime, timezone
            return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def on_bar(self, bar_time: Any, equity: float) -> dict[str, Any]:
        if not self.cfg["enabled"]:
            return {"enabled": False, "trading_paused": False}

        day = self._utc_day(bar_time)
        equity_f = float(equity)
        if self.current_day != day:
            if self.current_day is not None:
                self.days_tracked += 1
            self.current_day = day
            self.day_start_equity = equity_f
            self.trading_paused = False
            self.pause_reason = None

        self.peak_equity = max(self.peak_equity, equity_f)
        start = self.day_start_equity or equity_f
        daily_pnl_pct = ((equity_f - start) / start * 100.0) if start > 0 else 0.0
        target = self.cfg["daily_target_pct"]
        max_loss = self.cfg["max_daily_loss_pct"]

        if daily_pnl_pct >= target and self.cfg["lock_on_target"]:
            self.trading_paused = True
            self.pause_reason = "daily_target_hit"
            self.target_hit_days += 1
        elif daily_pnl_pct <= -max_loss:
            self.trading_paused = True
            self.pause_reason = "max_daily_loss"
            self.loss_pause_days += 1

        campaign_pnl_pct = (
            (equity_f - self.campaign_start_equity) / self.campaign_start_equity * 100.0
            if self.campaign_start_equity > 0 else 0.0
        )
        daily_mult = 1.0 + target / 100.0
        compound_target = self.campaign_start_equity * (daily_mult ** self.cfg["campaign_days"])

        snapshot = {
            "day": day,
            "equity": round(equity_f, 2),
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "daily_target_pct": target,
            "remaining_pct": round(max(0.0, target - daily_pnl_pct), 2),
            "campaign_pnl_pct": round(campaign_pnl_pct, 2),
            "compound_target_equity": round(compound_target, 2),
            "trading_paused": self.trading_paused,
            "pause_reason": self.pause_reason,
        }
        if not self.daily_log or self.daily_log[-1]["day"] != day:
            self.daily_log.append(snapshot)
        else:
            self.daily_log[-1] = snapshot
        return snapshot

    def should_block_entries(self) -> bool:
        return bool(self.cfg["enabled"] and self.trading_paused)

    def summary(self, final_equity: float) -> dict[str, Any]:
        equity_f = float(final_equity)
        campaign_pnl = round(equity_f - self.campaign_start_equity, 2)
        campaign_pnl_pct = round(
            (campaign_pnl / self.campaign_start_equity * 100.0)
            if self.campaign_start_equity > 0 else 0.0,
            2,
        )
        daily_mult = 1.0 + self.cfg["daily_target_pct"] / 100.0
        compound_target = round(
            self.campaign_start_equity * (daily_mult ** self.cfg["campaign_days"]), 2
        )
        on_track = equity_f >= self.campaign_start_equity
        return {
            "enabled": self.cfg["enabled"],
            "daily_target_pct": self.cfg["daily_target_pct"],
            "max_daily_loss_pct": self.cfg["max_daily_loss_pct"],
            "campaign_days": self.cfg["campaign_days"],
            "start_equity": round(self.campaign_start_equity, 2),
            "final_equity": round(equity_f, 2),
            "campaign_pnl": campaign_pnl,
            "campaign_pnl_pct": campaign_pnl_pct,
            "compound_target_equity": compound_target,
            "on_track": on_track,
            "positive_evolution": campaign_pnl > 0,
            "target_hit_days": self.target_hit_days,
            "loss_pause_days": self.loss_pause_days,
            "days_tracked": max(self.days_tracked, len(self.daily_log)),
            "peak_equity": round(self.peak_equity, 2),
            "daily_log": self.daily_log[-10:],
        }