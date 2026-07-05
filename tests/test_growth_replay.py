"""Tests for growth-awareness during historical replay."""

from __future__ import annotations

import os

from core.growth_replay import GrowthReplayTracker, growth_replay_settings
from core.profile_launcher import set_active_profile
from core.utils import load_config


def test_growth_replay_settings_from_micro_profile():
    os.environ["MT5_QUANT_PROFILE"] = "30"
    try:
        set_active_profile("30")
        cfg = load_config()
        settings = growth_replay_settings(cfg)
        assert settings["enabled"] is True
        assert settings["daily_target_pct"] == 20
        assert settings["max_daily_loss_pct"] == 10
        assert settings["start_equity"] == 30
    finally:
        os.environ.pop("MT5_QUANT_PROFILE", None)


def test_growth_tracker_pauses_on_daily_target():
    cfg = {
        "practice": {
            "growth": {"enabled": True, "daily_target_pct": 20, "max_daily_loss_pct": 10},
            "micro": {"enabled": True, "account_size_usd": 30},
        },
        "execution": {"starting_cash": 30},
    }
    tracker = GrowthReplayTracker(cfg, 30.0)
    tracker.on_bar("2026-06-01T10:00:00+00:00", 30.0)
    tracker.on_bar("2026-06-01T12:00:00+00:00", 36.5)
    assert tracker.should_block_entries() is True
    summary = tracker.summary(36.5)
    assert summary["target_hit_days"] >= 1
    assert summary["daily_target_pct"] == 20