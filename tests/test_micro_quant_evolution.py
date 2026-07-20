"""Tests for micro-account adaptive weight evolution (candidate 2)."""

from __future__ import annotations

import copy
import os

import pytest

from core.adaptive_weights import AdaptiveWeightOptimizer, load_weights
from core.edge_database import EdgeDatabase
from core.micro_quant_evolution import (
    micro_evolution_enabled,
    optimize_symbol_weights,
    replay_expectancy_usd,
    replay_fitness_score,
    run_micro_evolution,
)
from core.profile_launcher import set_active_profile
from core.utils import load_config, read_json_state


def _sample_trade(trade_id: str, symbol: str, result: str, tree: dict) -> dict:
    return {
        "trade_id": trade_id,
        "signal_id": f"sig-{trade_id}",
        "symbol": symbol,
        "side": "BUY",
        "setup_type": "trend_continuation",
        "entry": 2000.0,
        "exit": 2010.0 if result == "win" else 1990.0,
        "sl": 1990.0,
        "tp1": 2015.0,
        "pnl": 2.0 if result == "win" else -2.0,
        "result": result,
        "confidence": 75,
        "confidence_tree": tree,
        "market_context": {
            "session": "London",
            "market_regime": {"primary": "strong_trend"},
        },
    }


def _win_tree() -> dict:
    return {
        "trend_engine": 90,
        "structure_engine": 85,
        "momentum_engine": 70,
        "volume_engine": 60,
        "liquidity_engine": 55,
        "volatility_engine": 65,
        "risk_engine": 75,
    }


def _loss_tree() -> dict:
    return {
        "trend_engine": 35,
        "structure_engine": 40,
        "momentum_engine": 45,
        "volume_engine": 50,
        "liquidity_engine": 50,
        "volatility_engine": 45,
        "risk_engine": 40,
    }


def test_replay_fitness_and_expectancy():
    out = {"pnl_total": 6.0, "win_rate_pct": 60.0, "trades_closed": 3}
    assert replay_expectancy_usd(out) == 2.0
    assert replay_fitness_score(out) > 0


def test_optimize_symbol_weights(tmp_path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    db = EdgeDatabase()
    for i in range(10):
        db.ingest_trade(
            _sample_trade(
                f"x{i}",
                "XAUUSDm",
                "win" if i % 2 == 0 else "loss",
                _win_tree() if i % 2 == 0 else _loss_tree(),
            ),
            source="test",
        )

    records = db.load().get("records", [])
    result = optimize_symbol_weights(records, "XAUUSDm", min_trades=8)
    assert result["status"] == "candidate"
    assert abs(sum(result["weights"].values()) - 1.0) < 0.02


def test_micro_evolution_enabled_for_30_c2():
    cfg = {"quant": {"micro_evolution": {"enabled": True}}}
    assert micro_evolution_enabled(cfg)
    cfg2 = {"active_profile": "30-c2", "quant": {}}
    assert micro_evolution_enabled(cfg2)


def test_profile_30_c2_loads():
    os.environ["MT5_QUANT_PROFILE"] = "30-c2"
    try:
        set_active_profile("30-c2")
        cfg = load_config()
        assert cfg.get("active_profile") == "30-c2"
        assert cfg["quant"]["use_adaptive_weights"] is True
        assert cfg["quant"]["micro_evolution"]["enabled"] is True
        assert cfg["execution"]["starting_cash"] == 30
        assert cfg["mt5"]["symbols"] == ["XAUUSDm", "USOILm", "UK100m"]
    finally:
        os.environ.pop("MT5_QUANT_PROFILE", None)


def test_run_micro_evolution_hold_without_replay(monkeypatch, tmp_path):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    db = EdgeDatabase()
    for sym in ("XAUUSDm", "USOILm", "UK100m"):
        for i in range(10):
            db.ingest_trade(
                _sample_trade(
                    f"{sym}{i}",
                    sym,
                    "win" if i % 2 == 0 else "loss",
                    _win_tree() if i % 2 == 0 else _loss_tree(),
                ),
                source="test",
            )

    config = {
        "active_profile": "30-c2",
        "quant": {
            "use_adaptive_weights": True,
            "micro_evolution": {
                "enabled": True,
                "symbols": ["XAUUSDm", "USOILm", "UK100m"],
                "min_trades_per_symbol": 8,
                "improvement_threshold_pct": 99.0,
            },
        },
        "execution": {"starting_cash": 30, "mode": "paper"},
        "mt5": {"symbols": ["XAUUSDm", "USOILm", "UK100m"]},
        "replay": {},
        "features": {"min_bars_required": 20},
        "signals": {"min_confidence": 60},
        "trading": {},
        "risk": {},
    }

    class FakeReplay:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, **kwargs):
            return {"pnl_total": 1.0, "win_rate_pct": 55, "trades_closed": 5}

    monkeypatch.setattr("core.micro_quant_evolution.ReplayEngine", FakeReplay)

    out = run_micro_evolution(config)
    assert out["status"] == "ok"
    assert out["positive_evolution"] is False
    assert out["proposal"] == "hold"


def test_deploy_weights_stores_per_symbol(tmp_path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    candidate = {
        "weights": {"trend_engine": 0.3, "structure_engine": 0.2},
        "method": "micro_quant_evolution",
        "per_symbol": {
            "XAUUSDm": {
                "proposal": "deploy",
                "weights": {"trend_engine": 0.35, "structure_engine": 0.18},
                "improvement_pct": 8.0,
                "candidate_expectancy_usd": 0.12,
            },
        },
    }
    deployed = AdaptiveWeightOptimizer.deploy_weights(candidate)
    assert deployed["deployed"] is True
    assert "per_symbol" in deployed
    assert deployed["per_symbol"]["XAUUSDm"]["weights"]["trend_engine"] == 0.35


def test_load_weights_per_symbol(tmp_path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    utils.write_json_state("adaptive_weights.json", {
        "deployed": True,
        "weights": {"trend_engine": 0.22},
        "per_symbol": {
            "XAUUSDm": {"weights": {"trend_engine": 0.31, "structure_engine": 0.19}},
        },
    })

    cfg = {"quant": {"use_adaptive_weights": True}}
    sym_weights = load_weights(cfg, symbol="XAUUSDm")
    assert sym_weights["trend_engine"] == 0.31
    global_weights = load_weights(cfg)
    assert global_weights["trend_engine"] == 0.22