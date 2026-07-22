"""Tests for regime, setup library, consensus, and replay."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.consensus_gates import ConsensusGates
from core.market_regime import MarketRegimeEngine
from core.setup_library import SETUP_LIBRARY, is_regime_compatible
from core.replay_engine import ReplayEngine


@pytest.fixture
def config():
    from core.utils import load_config
    cfg = copy.deepcopy(load_config())
    cfg["execution"]["mode"] = "paper"
    cfg["replay"]["max_bars"] = 120
    cfg["replay"]["bars_per_step"] = 20
    return cfg


def test_market_regime_strong_trend():
    engine = MarketRegimeEngine()
    feat = {"m5_trend": "bearish", "bb_position": 0.3, "volatility_regime": "normal"}
    ctx = {
        "regime": "trending",
        "phase": "expansion",
        "move_type": "continuation",
        "trend_strength": "strong",
        "volatility_trend": "stable",
        "move_unusual": {"unusual": False},
        "market_intent": "bearish continuation",
    }
    result = engine.classify_symbol(feat, ctx)
    assert result["primary"] == "strong_trend"
    assert result["bias"] == "bearish"


def test_setup_regime_compatibility():
    assert is_regime_compatible("range_fade", "strong_trend") is False
    assert is_regime_compatible("trend_continuation", "strong_trend") is True
    assert len(SETUP_LIBRARY) >= 8


def test_consensus_regime_veto(config):
    config["intelligence"]["regime_veto_enabled"] = True
    gates = ConsensusGates(config)
    signal = {"symbol": "XAUUSDm", "side": "SELL", "setup_type": "range_fade"}
    votes = {"risk_engine": 80, "trend_engine": 70, "structure_engine": 70}
    passed, vetoes = gates.evaluate(
        signal,
        votes,
        market_regime={"primary": "strong_trend"},
    )
    assert passed is False
    assert any("regime_veto" in v for v in vetoes)


def test_replay_engine_runs(config):
    engine = ReplayEngine(config)
    result = engine.run(symbol="XAUUSDm")
    assert "bars_replayed" in result
    assert result["bars_replayed"] > 0
    assert "pnl_total" in result