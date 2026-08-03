"""Tests for Blue Guardian risk module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.blue_guardian import (
    apply_config_overrides,
    blue_guardian_enabled,
    can_modify_position_sl,
    cell_loss_stats,
    entry_gates,
    evaluate_daily_state,
    excess_position_close_actions,
    per_trade_close_actions,
    portfolio_close_all,
    position_age_seconds,
    total_floating_pnl,
)


def _config():
    return {
        "blue_guardian": {
            "enabled": True,
            "account_size_usd": 5000,
            "max_total_open_positions": 4,
            "max_open_per_symbol": 1,
            "floating_block_new_trades_usd": -35,
            "floating_close_all_usd": -45,
            "per_trade_normal_loss_usd": 25,
            "per_trade_hard_loss_usd": 30,
            "per_trade_emergency_usd": 35,
            "daily_loss_buffer_usd": 140,
            "max_daily_loss_usd": 150,
            "daily_profit_target_pct": 6,
            "daily_profit_target_usd": 300,
        },
        "trading": {"max_open_per_symbol": 3, "allow_pyramiding": True},
        "risk": {"max_drawdown_pct": 8},
        "signals": {"default_risk_percent": 2.5, "kelly_sizing": {"max_fraction": 5}},
        "practice": {"growth": {"daily_target_pct": 6, "max_daily_loss_pct": 4}},
    }


def test_enabled():
    assert blue_guardian_enabled(_config()) is True
    assert blue_guardian_enabled({}) is False


def test_entry_gates_max_total():
    positions = [{"symbol": f"S{i}m", "profit": 0} for i in range(4)]
    ok, code, _ = entry_gates(_config(), positions, {"symbol": "XAUUSDm"})
    assert ok is False
    assert code == "blue_guardian_max_total"


def test_one_trade_per_symbol_resolves_max_total():
    cfg = _config()
    cfg["mt5"] = {"symbols": ["XAUUSDm", "USOILm", "BTCUSDm"]}
    cfg["blue_guardian"]["one_trade_per_symbol"] = True
    from core.blue_guardian import blue_guardian_settings

    assert blue_guardian_settings(cfg)["max_total_open_positions"] == 3


def test_excess_position_close_actions():
    positions = [
        {"ticket": 1, "symbol": "USOILm", "side": "SELL", "size": 0.1, "profit": -1.0},
        {"ticket": 2, "symbol": "UK100m", "side": "BUY", "size": 0.05, "profit": 0.3},
        {"ticket": 3, "symbol": "US30m", "side": "BUY", "size": 0.05, "profit": 1.0},
        {"ticket": 4, "symbol": "US500m", "side": "BUY", "size": 0.05, "profit": 0.5},
        {"ticket": 5, "symbol": "FR40m", "side": "BUY", "size": 0.05, "profit": 0.8},
        {"ticket": 6, "symbol": "NAS100m", "side": "BUY", "size": 0.01, "profit": 0.4},
    ]
    actions = excess_position_close_actions(_config(), positions)
    assert len(actions) == 2
    assert actions[0]["ticket"] == 1
    assert actions[0]["reason"] == "blue_guardian_excess_positions"


def test_entry_gates_floating_block():
    positions = [{"symbol": "XAUUSDm", "profit": -36}]
    ok, code, _ = entry_gates(_config(), positions, {"symbol": "USOILm"})
    assert ok is False
    assert code == "blue_guardian_floating_block"


def test_portfolio_flatten():
    positions = [{"symbol": "XAUUSDm", "profit": -50, "ticket": 1, "side": "SELL", "size": 0.03}]
    action = portfolio_close_all(_config(), positions)
    assert action is not None
    assert action["floating_pnl"] == -50


def test_per_trade_actions():
    positions = [
        {"ticket": 1, "symbol": "XAUUSDm", "side": "SELL", "size": 0.03, "profit": -26},
        {"ticket": 2, "symbol": "USOILm", "side": "BUY", "size": 0.05, "profit": 5},
    ]
    actions = per_trade_close_actions(_config(), positions)
    assert len(actions) == 1
    assert actions[0]["reason"] == "blue_guardian_normal_loss"


def test_apply_overrides():
    cfg = apply_config_overrides(_config())
    assert cfg["trading"]["max_open_per_symbol"] == 1
    assert cfg["trading"]["allow_pyramiding"] is False
    assert cfg["signals"]["default_risk_percent"] == 0.5


def test_apply_overrides_preserves_configured_ranking_policy():
    """Guardian risk overrides must not silently disable top-N ranking."""
    cfg = _config()
    cfg["quant"] = {
        "strategy_ranking_enabled": True,
        "require_top_ranked_setup": True,
        "ranking_flex_enabled": True,
    }
    cfg["practice"]["growth"]["strategy_ranking_enabled"] = True

    out = apply_config_overrides(cfg)
    assert out["quant"]["strategy_ranking_enabled"] is True
    assert out["quant"]["require_top_ranked_setup"] is True
    assert out["quant"]["ranking_flex_enabled"] is True


def test_cell_loss_stats():
    trades = [{"pnl": -30}, {"pnl": -20}, {"pnl": 10}]
    stats = cell_loss_stats(trades)
    assert stats["breach_25_count"] == 1
    assert stats["max_loss_usd"] == -30


def test_total_floating():
    assert total_floating_pnl([{"profit": 1.5}, {"profit": -2}]) == -0.5


def test_position_age_prefers_agent_open_time_over_mt5():
    """MT5 pos.time is broker-local; agent open_times are true UTC."""
    agent_open = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    future_mt5 = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    pos = {"ticket": 21611685, "opened_at": future_mt5}
    open_times = {"21611685": agent_open}
    age = position_age_seconds(pos, open_times)
    assert age >= 600 - 2
    assert age < 3600


def test_can_modify_sl_after_min_hold_when_open_time_known():
    cfg = _config()
    cfg["blue_guardian"]["min_hold_seconds"] = 130
    agent_open = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    pos = {"ticket": 99, "opened_at": agent_open}
    assert can_modify_position_sl(cfg, pos) is True


def test_future_mt5_opened_at_blocks_sl_without_agent_time():
    cfg = _config()
    cfg["blue_guardian"]["min_hold_seconds"] = 130
    future_mt5 = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    pos = {"ticket": 21611685, "opened_at": future_mt5}
    assert position_age_seconds(pos, open_times={}) == 0.0
    assert can_modify_position_sl(cfg, pos) is False