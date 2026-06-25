"""End-to-end test with crafted features to validate signal → verify → execute chain."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.paper_broker import PaperBroker
from core.signal_engine import SignalEngine
from core.utils import load_config, write_json_state
from core.verifier import Verifier


def test_signal_verify_execute_chain():
    config = load_config()

    features = {
        "timestamp": "2026-06-25T00:00:00+00:00",
        "symbols": {
            "XAUUSDm": {
                "symbol": "XAUUSDm",
                "price": 2650.0,
                "m5_trend": "bearish",
                "m15_trend": "bearish",
                "bb_upper": 2660.0,
                "bb_middle": 2650.0,
                "bb_lower": 2640.0,
                "bb_position": 0.85,
                "stoch_k": 78.0,
                "stoch_d": 72.0,
                "stoch_cross": "bearish_cross",
                "atr": 5.0,
                "atr_ratio": 0.0019,
                "volume_avg": 1000,
                "volume_ratio": 2.0,
                "support": 2640.0,
                "resistance": 2660.0,
                "rejection": "bearish_rejection",
                "breakout": "none",
                "timeframe_alignment": True,
                "volatility_regime": "normal",
            }
        },
    }

    engine = SignalEngine(config)
    candidates = engine.generate_candidates(features, {"setups": {}})
    assert len(candidates) > 0

    verifier = Verifier(config)
    approved, rejected = verifier.verify_batch(
        candidates,
        features,
        spread_data={"XAUUSDm": 20.0},
    )
    assert len(approved) + len(rejected) == len(candidates)

    if approved:
        paper_config = copy.deepcopy(config)
        paper_config["execution"]["mode"] = "paper"
        broker = PaperBroker(paper_config)
        result = broker.process_approved_signals(
            approved,
            {"XAUUSDm": 2650.0},
        )
        assert result["mode"] == "paper"
        write_json_state("test_e2e_ok.json", {"approved": len(approved), "positions": len(result["positions"])})