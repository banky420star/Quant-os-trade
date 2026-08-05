"""Policy detection loop tests — shadow scoring and sample gates."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.policy_detection import (
    build_policy_variants,
    cell_key,
    detect_policies,
    policy_detection_enabled,
    policy_detection_mode,
    score_variant,
)
from core.state_store import StateStore


def _sample_trade(**overrides) -> dict:
    base = {
        "trade_id": "t1",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "pullback",
        "session": "rollover",
        "result": "win",
        "pnl": 1.2,
        "r_multiple": 1.1,
        "management_profile": {
            "entry_type": "limit",
            "limit_offset_atr": 0.15,
            "sl_atr_mult": 1.0,
            "tp1_r": 0.8,
            "break_even_trigger_r": 0.35,
            "trail_start_r": 0.55,
        },
    }
    base.update(overrides)
    return base


def _sample_signal(**overrides) -> dict:
    base = {
        "signal_id": "sig-1",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "pullback",
        "confidence": 80,
        "entry_quality": 70,
        "within_reach": True,
        "distance_atr": 0.3,
        "market_context": {"session": "rollover"},
    }
    base.update(overrides)
    return base


def test_build_policy_variants_grid():
    variants = build_policy_variants()
    assert len(variants) == 12
    entry_types = {v["entry_type"] for v in variants}
    assert entry_types == {"market", "limit"}
    offsets = sorted({v["limit_offset_atr"] for v in variants if v["entry_type"] == "limit"})
    assert offsets == [0.05, 0.10, 0.20]


def test_policy_detection_shadow_mode(growth_profile):
    from core.utils import load_config

    config = load_config()
    assert policy_detection_enabled(config)
    assert policy_detection_mode(config) == "shadow"

    trades = {"trades": [_sample_trade(), _sample_trade(trade_id="t2", result="loss", pnl=-0.8)]}
    evaluated = {"evaluated": [_sample_signal()]}
    features = {
        "symbols": {
            "XAUUSDm": {"atr": 4.0, "volume_ratio": 1.1, "spread_points": 25},
        },
    }
    doc = detect_policies(trades, evaluated, features, config)
    assert doc["mode"] == "shadow"
    assert doc["cell_count"] >= 1
    assert len(doc["variants"]) >= 12
    key = cell_key("XAUUSDm", "pullback", "rollover")
    assert key in doc["best_by_key"]
    best = doc["best_by_key"][key]
    assert best["symbol"] == "XAUUSDm"
    assert best["setup_type"] == "pullback"
    assert best["session"] == "rollover"
    assert "policy_id" in best
    assert best["sample_n"] == 2


def test_sample_gate_penalizes_thin_data(growth_profile):
    from core.utils import load_config

    config = load_config()
    config["policy_detection"]["min_sample_n"] = 5
    variant = build_policy_variants()[0]
    thin_score, thin_n = score_variant(
        variant,
        symbol="XAUUSDm",
        setup_type="pullback",
        session="rollover",
        cell_trades=[_sample_trade()],
        config=config,
    )
    rich_trades = [_sample_trade(trade_id=f"t{i}") for i in range(6)]
    rich_score, rich_n = score_variant(
        variant,
        symbol="XAUUSDm",
        setup_type="pullback",
        session="rollover",
        cell_trades=rich_trades,
        config=config,
    )
    assert thin_n == 1
    assert rich_n == 6
    assert rich_score > thin_score


def test_winning_profile_boosts_matching_variant(growth_profile):
    from core.utils import load_config

    config = load_config()
    trades = [
        _sample_trade(),
        _sample_trade(trade_id="t2"),
        _sample_trade(
            trade_id="t3",
            result="loss",
            pnl=-1.0,
            management_profile={
                "entry_type": "market",
                "sl_atr_mult": 1.2,
                "tp1_r": 1.0,
                "break_even_trigger_r": 0.45,
                "trail_start_r": 0.70,
            },
        ),
    ]
    match = {
        "entry_type": "limit",
        "limit_offset_atr": 0.05,
        "sl_atr_mult": 1.0,
        "tp1_r": 0.8,
        "be_trigger_r": 0.35,
        "trail_start_r": 0.55,
    }
    mismatch = {
        "entry_type": "market",
        "limit_offset_atr": 0.0,
        "sl_atr_mult": 1.2,
        "tp1_r": 1.0,
        "be_trigger_r": 0.45,
        "trail_start_r": 0.70,
    }
    match_score, _ = score_variant(
        match,
        symbol="XAUUSDm",
        setup_type="pullback",
        session="rollover",
        cell_trades=trades,
        config=config,
    )
    mismatch_score, _ = score_variant(
        mismatch,
        symbol="XAUUSDm",
        setup_type="pullback",
        session="rollover",
        cell_trades=trades,
        config=config,
    )
    assert match_score > mismatch_score


def test_policy_scores_sqlite_roundtrip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "policy.db")
    doc = {
        "timestamp": "2026-07-07T12:00:00+00:00",
        "mode": "shadow",
        "variants": [
            {
                "policy_id": "pol_test01",
                "symbol": "XAUUSDm",
                "setup_type": "pullback",
                "session": "rollover",
                "entry_type": "limit",
                "limit_offset_atr": 0.1,
                "sl_atr_mult": 1.0,
                "tp1_r": 0.8,
                "be_trigger_r": 0.35,
                "trail_start_r": 0.55,
                "score": 72.5,
                "sample_n": 4,
            },
        ],
        "best_by_key": {
            "XAUUSDm|pullback|rollover": {
                "policy_id": "pol_test01",
                "symbol": "XAUUSDm",
                "score": 72.5,
                "sample_n": 4,
            },
        },
    }
    store.write_policy_scores(doc)
    out = store.read_policy_scores()
    assert out["mode"] == "shadow"
    assert out["variant_count"] == 1
    assert out["variants"][0]["policy_id"] == "pol_test01"
    assert out["variants"][0]["score"] == pytest.approx(72.5)


def test_policy_detection_loop_writes_json(tmp_path: Path, monkeypatch, growth_profile):
    from core import utils
    from core.trade_history import trade_history_filename
    from core.utils import load_config
    from loops import policy_detection_loop

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    (state_dir / "features.json").write_text(
        json.dumps({"symbols": {"XAUUSDm": {"atr": 4.0, "volume_ratio": 1.0}}}),
        encoding="utf-8",
    )
    (state_dir / trade_history_filename(load_config())).write_text(
        json.dumps({"trades": [_sample_trade()]}),
        encoding="utf-8",
    )
    (state_dir / "evaluated_signals.json").write_text(
        json.dumps({"evaluated": [_sample_signal()]}),
        encoding="utf-8",
    )

    doc = policy_detection_loop.run()
    assert doc is not None
    assert doc["mode"] == "shadow"
    saved = json.loads((state_dir / "policy_scores.json").read_text(encoding="utf-8"))
    assert saved["mode"] == "shadow"
    assert len(saved["variants"]) >= 12