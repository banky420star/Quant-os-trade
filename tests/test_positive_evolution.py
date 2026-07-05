"""Tests for Culturing Evolution positive-expectancy gates."""

from __future__ import annotations

import copy
import os

import pytest

from core.positive_evolution import (
    build_positive_evolution_state,
    diff_positive_cells,
    evaluate_cell_gate,
    positive_evolution_active,
    positive_evolution_settings,
    refresh_positive_evolution,
)
from core.profile_launcher import set_active_profile
from core.strategy_policy import culturing_cell_key
from core.utils import load_config
from core.verifier import Verifier


def _micro_config_with_pe():
    os.environ["MT5_QUANT_PROFILE"] = "30-c1"
    set_active_profile("30-c1")
    return load_config()


def test_profile_30_c1_positive_evolution_enabled():
    config = _micro_config_with_pe()
    assert config.get("active_profile") == "30-c1"
    assert positive_evolution_active(config)
    pe = positive_evolution_settings(config)
    assert pe["enabled"] is True
    assert pe["min_n"] == 6
    assert config["mt5"]["symbols"] == ["XAUUSDm", "USOILm", "UK100m"]
    assert config["execution"]["max_lot"] == 0.01
    assert config["risk"]["max_loss_per_trade_usd"] == 10
    assert config["risk"]["cap_loss_to_balance"] is True
    assert config["trading"]["allow_pyramiding"] is False


def test_build_positive_evolution_from_ledger():
    config = {
        "practice": {"micro": {"positive_evolution_enabled": True, "symbols": ["XAUUSDm"]}},
        "culturing": {"min_n": 6},
        "quant": {"positive_evolution": {"enabled": True, "min_n": 6}},
    }
    ledger_cells = {
        "XAUUSDm": {
            "pullback|strong_trend|align|london_open": {
                "n": 10,
                "expectancy_net_r": 0.25,
                "win_rate_pct": 60,
            },
            "range_fade|range|counter|asia": {
                "n": 8,
                "expectancy_net_r": -0.15,
                "win_rate_pct": 35,
            },
            "breakout|expansion|align|new_york": {
                "n": 3,
                "expectancy_net_r": -0.5,
                "win_rate_pct": 20,
            },
        }
    }
    state = build_positive_evolution_state(config, ledger_cells=ledger_cells)
    sym = state["symbols"]["XAUUSDm"]
    assert "pullback|strong_trend|align|london_open" in sym["allowed_cells"]
    assert "range_fade|range|counter|asia" in sym["blocked_cells"]
    assert state["total_allowed"] >= 1
    assert state["total_blocked"] >= 1


def test_evaluate_cell_gate_blocks_non_positive(tmp_path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    config = {
        "practice": {"micro": {"positive_evolution_enabled": True, "symbols": ["XAUUSDm"]}},
        "quant": {"positive_evolution": {"enabled": True, "min_n": 6}},
        "culturing": {"min_n": 6},
    }
    ledger = {
        "cells": {
            "XAUUSDm": {
                "bad|range|counter|asia": {"n": 10, "expectancy_net_r": -0.2},
                "good|trend|align|london_open": {"n": 10, "expectancy_net_r": 0.3},
            }
        }
    }
    utils.write_json_state("forward_test_ledger.json", ledger)

    allowed_good, _ = evaluate_cell_gate(config, "XAUUSDm", "good|trend|align|london_open")
    allowed_bad, reason_bad = evaluate_cell_gate(config, "XAUUSDm", "bad|range|counter|asia")
    allowed_thin, _ = evaluate_cell_gate(config, "XAUUSDm", "thin|range|align|asia")

    assert allowed_good is True
    assert allowed_bad is False
    assert reason_bad == "non_positive_expectancy"
    assert allowed_thin is True


def test_verifier_rejects_non_positive_cell(tmp_path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    config = copy.deepcopy(load_config())
    config["practice"]["micro"]["positive_evolution_enabled"] = True
    config["quant"]["positive_evolution_enabled"] = True
    config["quant"]["positive_evolution"] = {"enabled": True, "min_n": 6}
    config["mt5"]["symbols"] = ["XAUUSDm"]

    utils.write_json_state("forward_test_ledger.json", {
        "cells": {
            "XAUUSDm": {
                "trend_continuation|strong_trend|align|london_open": {
                    "n": 12,
                    "expectancy_net_r": -0.1,
                    "win_rate_pct": 42,
                }
            }
        }
    })
    utils.write_json_state("positive_evolution.json", {
        "symbols": {
            "XAUUSDm": {
                "allowed_cells": [],
                "blocked_cells": ["trend_continuation|strong_trend|align|london_open"],
            }
        }
    })
    utils.write_json_state("symbol_policy_live.json", {"symbols": {"XAUUSDm": {"vetoed_cells": []}}})

    signal = {
        "signal_id": "sig-pe-1",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "trend_continuation",
        "entry": 2000.0,
        "sl": 1990.0,
        "tp1": 2020.0,
        "confidence": 80,
        "market_context": {
            "session": "london_open",
            "market_regime": {"primary": "strong_trend", "bias": "bullish"},
        },
    }
    feat = {"atr_ratio": 0.01, "volume_ratio": 1.0, "m5_trend": "bullish", "m15_trend": "bullish"}
    config["trading"]["aggressive_mode"] = True
    config["trading"]["entry_confirm_seconds"] = 0
    verifier = Verifier(config)
    result = verifier._verify_one(signal, feat, [], False, 50.0, 30.0, [])
    assert result["checks"]["positive_evolution"] is False
    assert "positive_evolution" in result["failure_codes"]


def test_diff_positive_cells_tracks_evolution():
    old = {
        "symbols": {
            "USOILm": {"allowed_cells": ["a"], "blocked_cells": []},
        }
    }
    new = {
        "symbols": {
            "USOILm": {
                "allowed_cells": ["a", "b"],
                "blocked_cells": ["c"],
            }
        }
    }
    diff = diff_positive_cells(old, new)
    assert any(x["cell"] == "b" and x["kind"] == "allowed" for x in diff["added"])
    assert any(x["cell"] == "c" and x["kind"] == "blocked" for x in diff["added"])


def test_refresh_positive_evolution_writes_state(tmp_path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    config = _micro_config_with_pe()
    utils.write_json_state("forward_test_ledger.json", {"cells": {}})
    payload = refresh_positive_evolution(config)
    assert payload.get("enabled") is True
    saved = utils.read_json_state("positive_evolution.json")
    assert saved.get("enabled") is True