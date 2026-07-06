"""Acceptance tests for MT5 quant agent loops."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATE = ROOT / "state"
LOGS = ROOT / "logs"


@pytest.fixture(scope="module", autouse=True)
def run_pipeline_once():
    """Run one pipeline cycle before acceptance tests (no infinite loop)."""
    from core.pipeline import run_pipeline
    from core.utils import load_config, setup_logger
    from run_all import update_state_md

    config = load_config()
    logger = setup_logger("test_system", "system.log")
    result = run_pipeline(config, logger)
    update_state_md(result.get("loops", {}), cycle=0)


def test_config_exists():
    assert (ROOT / "config.yaml").exists()


def test_latest_candles():
    path = STATE / "latest_candles.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "symbols" in data
    assert "timestamp" in data


def test_features():
    path = STATE / "features.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "symbols" in data
    for symbol, feat in data["symbols"].items():
        assert "price" in feat
        assert "m5_trend" in feat
        assert "atr" in feat


def test_candidate_signals():
    path = STATE / "candidate_signals.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "candidates" in data


def test_verifier_outputs():
    assert (STATE / "approved_signals.json").exists()
    assert (STATE / "rejected_signals.json").exists()


def test_paper_execution():
    assert (STATE / "paper_orders.json").exists()
    assert (STATE / "paper_positions.json").exists()
    assert (STATE / "paper_trades.json").exists()


def test_risk_outputs():
    path = STATE / "risk_state.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "kill_switch" in data
    assert (STATE / "kill_switch.json").exists()


def test_memory_outputs():
    assert (STATE / "memory.json").exists()
    assert (STATE / "edge_scores.json").exists()


def test_logs_created():
    for log_file in (
        "data_loop.log",
        "feature_loop.log",
        "signal_loop.log",
        "verifier_loop.log",
        "execution_loop.log",
        "risk_loop.log",
        "memory_loop.log",
        "system.log",
    ):
        assert (LOGS / log_file).exists(), f"Missing log: {log_file}"


def test_state_md_updated():
    content = (ROOT / "docs" / "STATE.md").read_text(encoding="utf-8")
    assert "Last run:" in content
    assert "## Loop Status" in content
    assert "**Mode:**" in content


def test_live_trading_disabled():
    from core.utils import load_config
    config = load_config()
    assert config["execution"]["live_trading_enabled"] is False
    assert config["execution"].get("allow_live_account") is False