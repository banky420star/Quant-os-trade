"""Daily growth plan tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.account_mode import runtime_mode_summary
from core.daily_growth import evaluate_daily_growth, growth_plan_enabled, sync_daily_session
from core.risk_manager import RiskManager
from core.utils import load_config, write_json_state


@pytest.fixture
def growth_config(monkeypatch):
    monkeypatch.setenv("MT5_QUANT_PROFILE", "growth")
    cfg = load_config()
    cfg.setdefault("practice", {}).setdefault("growth", {})["enabled"] = True
    cfg["practice"]["growth"]["daily_target_pct"] = 20
    cfg["practice"]["growth"]["max_daily_loss_pct"] = 10
    return cfg


def test_growth_plan_enabled_on_demo(growth_config):
    assert growth_plan_enabled(growth_config) is True
    mode = runtime_mode_summary(growth_config)
    assert mode["label"] in ("growth", "micro_growth")
    assert mode["growth_plan_active"] is True
    if mode.get("micro_profile_active"):
        assert mode["daily_target_pct"] in (20, 35)
    else:
        assert mode["daily_target_pct"] == 20


def test_sync_practice_gates_applies_growth_risk(growth_config):
    write_json_state("account.json", {"equity": 100.0, "balance": 100.0})
    from core.practice_session import sync_practice_gates

    cfg = sync_practice_gates(growth_config)
    micro_on = bool((cfg.get("practice") or {}).get("micro", {}).get("enabled"))
    if micro_on:
        assert cfg["signals"]["default_risk_percent"] == 1.0
        assert cfg["practice"]["growth"]["enabled"] is True
    else:
        assert cfg["signals"]["default_risk_percent"] == 2.5
        assert cfg["quant"]["strategy_ranking_enabled"] is False
        assert cfg["trading"]["aggressive_mode"] is True
        assert cfg["session_scoring"]["enabled"] is False
        exp_frac = float((cfg.get("practice") or {}).get("growth", {}).get("max_total_exposure_fraction", 0.92))
        assert cfg["risk"]["max_total_exposure_usd"] == pytest.approx(100.0 * exp_frac, rel=0.01)


def test_continuous_campaign_skips_daily_lock(growth_config, monkeypatch):
    growth_config["practice"]["growth"]["lock_profit_when_target_hit"] = False
    growth_config["practice"]["growth"]["continuous_through_campaign"] = True
    growth_config["practice"]["growth"]["campaign_days"] = 30
    monkeypatch.setattr("core.daily_growth._utc_day", lambda: "2099-01-01")
    monkeypatch.setattr("core.daily_growth.write_json_state", lambda name, data: None)
    monkeypatch.setattr(
        "core.daily_growth.read_json_state",
        lambda name, default=None: {
            "day": "2099-01-01",
            "day_start_equity": 100.0,
            "target_pct": 20,
        },
    )
    out = evaluate_daily_growth(125.0, growth_config)
    assert out["target_hit"] is True
    assert out["trading_paused"] is False


def test_daily_target_hit_pauses_trading(growth_config, monkeypatch):
    growth_config["practice"]["growth"]["lock_profit_when_target_hit"] = True
    growth_config["practice"]["growth"]["continuous_through_campaign"] = False
    monkeypatch.setattr("core.daily_growth._utc_day", lambda: "2099-01-01")
    monkeypatch.setattr("core.daily_growth.write_json_state", lambda name, data: None)
    monkeypatch.setattr(
        "core.daily_growth.read_json_state",
        lambda name, default=None: {
            "day": "2099-01-01",
            "day_start_equity": 100.0,
            "target_pct": 20,
        },
    )
    out = evaluate_daily_growth(125.0, growth_config)
    assert out["daily_pnl_pct"] == 25.0
    assert out["target_hit"] is True
    assert out["trading_paused"] is True
    assert "Daily target" in (out.get("pause_reason") or "")


def test_risk_manager_surfaces_daily_growth(growth_config):
    write_json_state("account.json", {"equity": 100.0, "balance": 100.0, "starting_cash": 100.0})
    rm = RiskManager(growth_config)
    result = rm.evaluate(
        positions=[],
        orders=[],
        balance={"equity": 100.0, "cash": 100.0, "starting_cash": 100.0},
    )
    assert "daily_growth" in result["risk_state"]
    assert result["risk_state"]["daily_growth"]["enabled"] is True