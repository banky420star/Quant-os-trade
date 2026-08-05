"""Regression tests for the live-DEMO all-strategy data-lab profile (2026-08-04).

Previously paper-only; now execution.mode=mt5 with live/mt5 trading enabled so
every strategy stream (main + specialized setups) reaches the MT5 order path on
the demo account. Risk caps still bind.
"""

from __future__ import annotations

from core.account_mode import runtime_mode_summary
from core.profile_guard import assert_profile
from core.profile_launcher import load_profile_overlay
from core.utils import load_config


EXPECTED_SYMBOLS = list(load_profile_overlay("data-lab")["practice"]["symbols"])


def test_data_lab_overlay_is_live_demo_and_broad(monkeypatch):
    monkeypatch.setenv("MT5_QUANT_PROFILE", "data-lab")

    config = load_config()

    assert config["active_profile"] == "data-lab"
    assert config["mode"] == "practice"
    # MT5 order path ON (demo account).
    assert config["execution"]["mode"] == "mt5"
    assert config["execution"]["live_trading_enabled"] is True
    assert config["execution"]["mt5_trading_enabled"] is True
    assert config["execution"]["allow_live_account"] is False  # demo-only safety
    assert config["execution"]["fixed_exit_only"] is True
    assert config["execution"]["strategy_entries_market_only"] is True
    assert config["execution"]["fast_mode_enabled"] is False
    assert config["execution"]["max_lot"] == 100.0
    assert config["mt5"]["account_mode"] == "demo"
    assert config["mt5"]["symbols"] == EXPECTED_SYMBOLS
    assert config["practice"]["symbols"] == EXPECTED_SYMBOLS
    assert config["strategy_arena"]["symbols"] == EXPECTED_SYMBOLS
    assert config["strategy_arena"]["emit_all_setups"] is True
    # All specialized setups emit candidates AND log fires (enabled=true +
    # shadow=true = emit + log per detect_specialized) so the culturing
    # evidence stream stays alive while they trade live on demo.
    assert config["signals"]["specialized_setups"]["enabled"] is True
    assert config["signals"]["specialized_setups"]["shadow"] is True
    assert config["evaluation"]["min_policy_score"] == 0
    assert config["evaluation"]["setup_blocklist"] == []
    assert config["quant"]["strategy_ranking_enabled"] is False
    assert config["quant"]["require_top_ranked_setup"] is False
    assert config["signals"]["kelly_sizing"]["kelly_fraction"] == 1.0
    assert config["signals"]["kelly_sizing"]["min_n"] == 20
    assert config["signals"]["kelly_sizing"]["max_fraction"] == 5.0
    assert config["signals"]["kelly_sizing"]["fallback_fraction"] == 0.5
    assert config["signals"]["kelly_sizing"]["evidence_min_trades"] == 20
    assert config["signals"]["kelly_sizing"]["symbol_aggregate_fallback"] is False
    assert config["risk"]["allow_full_kelly"] is True
    assert config["risk"]["max_risk_per_trade_pct"] == 5.0
    assert config["risk"]["enforce_equity_risk_cap"] is True
    # Paper-only mechanisms off.
    assert config["forward_labeling"]["enabled"] is False
    assert config["self_learning"]["shadow_experiments"]["routing_enabled"] is False

    assert_profile(config)  # demo + practice mode is a safe combination
    assert runtime_mode_summary(config)["label"] == "practice"


def test_data_lab_file_declares_live_demo_mode():
    overlay = load_profile_overlay("data-lab")

    assert overlay["mode"] == "practice"
    assert overlay["execution"]["mode"] == "mt5"
    assert overlay["execution"]["live_trading_enabled"] is True
    assert overlay["execution"]["mt5_trading_enabled"] is True
    assert overlay["execution"]["allow_live_account"] is False
    assert overlay["execution"]["fixed_exit_only"] is True
    assert overlay["execution"]["strategy_entries_market_only"] is True
    assert overlay["execution"]["fast_mode_enabled"] is False
    assert overlay["execution"]["max_lot"] == 100.0
    assert overlay["mt5"]["account_mode"] == "demo"
    assert overlay["practice"]["micro"]["enabled"] is False
    assert overlay["practice"]["growth"]["enabled"] is False
    # Specialized setups all on, shadow on = emit AND log (live + evidence).
    assert overlay["signals"]["specialized_setups"]["enabled"] is True
    assert overlay["signals"]["specialized_setups"]["shadow"] is True
