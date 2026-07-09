"""Evaluation policy layer tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.evaluation_policy import evaluate_batch, evaluate_candidate
from core.exit_manager import near_take_profit
from core.policy_score import compute_policy_score, session_entry_bias


@pytest.fixture
def growth_profile(monkeypatch):
    monkeypatch.setenv("MT5_QUANT_PROFILE", "growth")


def _sample_signal(**overrides) -> dict:
    base = {
        "signal_id": "sig-test",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "pullback",
        "confidence": 85,
        "entry": 2400.0,
        "sl": 2390.0,
        "tp1": 2420.0,
        "entry_quality": 72,
        "within_reach": True,
        "distance_atr": 0.3,
        "entry_mode": "limit",
        "market_context": {"session": "rollover", "regime": "trending"},
    }
    base.update(overrides)
    return base


def test_losing_exit_not_labelled_take_profit():
    """Loss on BUY must not match TP even if price is vaguely near TP level."""
    tp = 10742.77
    exit_price = 10681.84
    assert not near_take_profit(exit_price, tp, side="BUY")
    assert not near_take_profit(exit_price, tp, side="BUY", point=0.01)


def test_winning_exit_near_tp_buy():
    assert near_take_profit(2420.0, 2420.5, side="BUY", point=0.01)


def test_evaluation_attaches_management_profile(growth_profile):
    from core.utils import load_config

    config = load_config()
    feat = {"price": 2400.0, "atr": 4.0, "volume_ratio": 1.1, "m5_trend": "bullish"}
    out = evaluate_candidate(_sample_signal(), feat, config)
    assert out.get("management_profile")
    assert out["management_profile"]["break_even_trigger_r"] == pytest.approx(0.35)
    assert out["execution_policy"]["entry_type"] in ("limit", "market")
    assert out["evaluation"]["policy_score"] > 0


def test_low_score_marks_skip(growth_profile):
    from core.utils import load_config

    config = load_config()
    config["evaluation"]["skip_below_score"] = 99
    feat = {"price": 2400.0, "atr": 4.0, "volume_ratio": 0.5}
    out = evaluate_candidate(
        _sample_signal(within_reach=False, distance_atr=2.5, entry_quality=20),
        feat,
        config,
    )
    assert out["evaluation"]["action"] == "skip"


def test_limit_chosen_when_drifted_from_anchor(growth_profile):
    from core.utils import load_config

    config = load_config()
    feat = {"price": 2405.0, "atr": 4.0, "volume_ratio": 0.9}
    out = evaluate_candidate(
        _sample_signal(distance_atr=0.9, within_reach=True, market_context={"session": "tokyo"}),
        feat,
        config,
    )
    assert out["execution_policy"]["entry_type"] == "limit"


def test_evaluate_batch_splits_skipped(growth_profile):
    from core.utils import load_config

    config = load_config()
    config["evaluation"]["skip_below_score"] = 99
    features = {"symbols": {"XAUUSDm": {"atr": 4.0, "volume_ratio": 1.0}}}
    evaluated, skipped = evaluate_batch(
        [_sample_signal(), _sample_signal(symbol="USOILm", within_reach=False, distance_atr=3.0)],
        features,
        config,
    )
    assert len(skipped) >= 1


def test_rollover_session_bias_is_cautious():
    bias = session_entry_bias("rollover")
    assert bias["entry_type"] == "limit"
    assert bias["break_even_trigger_r"] == pytest.approx(0.35)


def test_policy_score_penalizes_weak_volume():
    score, reasons = compute_policy_score(
        _sample_signal(),
        {"volume_ratio": 0.4},
        session_bias=session_entry_bias("rollover"),
    )
    assert score < 60
    assert any("volume_weak" in r for r in reasons)



def _cold_recent_trades(symbol="UK100m", n=10):
    return [{"symbol": symbol, "result": "loss", "pnl": -0.02} for _ in range(n)]


def test_recent_cold_streak_skips_symbol(growth_profile):
    """A symbol that lost its last 8+ trades (win<20%) is skipped despite high entry quality."""
    from core.utils import load_config

    config = load_config()
    config["evaluation"]["recent_cold_skip_win_rate"] = 20
    config["evaluation"]["recent_cold_skip_min_n"] = 8
    feat = {"price": 2400.0, "atr": 4.0, "volume_ratio": 1.2}
    out = evaluate_candidate(
        _sample_signal(symbol="UK100m", entry_quality=99, confidence=80, within_reach=True, distance_atr=0.0),
        feat,
        config,
        recent_trades=_cold_recent_trades("UK100m", 10),
    )
    assert out["evaluation"]["action"] == "skip"
    assert "recent_cold_symbol" in out["evaluation"]["reason"]


def test_symbol_blocklist_skips(growth_profile):
    """Explicitly blocklisted symbols are skipped even with a perfect score."""
    from core.utils import load_config

    config = load_config()
    config["evaluation"]["symbol_blocklist"] = ["UK100m"]
    feat = {"price": 2400.0, "atr": 4.0, "volume_ratio": 1.2}
    out = evaluate_candidate(
        _sample_signal(symbol="UK100m", entry_quality=99, confidence=90, within_reach=True, distance_atr=0.0),
        feat,
        config,
        recent_trades=[],
    )
    assert out["evaluation"]["action"] == "skip"
    assert "symbol_blocklist" in out["evaluation"]["reason"]


def test_global_edge_cold_blocks_known_bad_cell(growth_profile, monkeypatch):
    """When the recent sample is thin, a historically-bad symbol+setup cell is blocked."""
    from core.utils import load_config

    config = load_config()
    config["evaluation"]["global_edge_cold_win_rate"] = 20
    config["evaluation"]["global_edge_cold_min_n"] = 6
    monkeypatch.setattr(
        "core.utils.read_json_state",
        lambda name, default=None: (
            {"setup_stats": {"by_symbol": {"UK100m": {"pullback": {"wins": 0, "losses": 23, "total": 23, "win_rate_pct": 0.0}}}}}
            if name == "edge_scores.json"
            else (default or {})
        ),
    )
    feat = {"price": 2400.0, "atr": 4.0, "volume_ratio": 1.2}
    out = evaluate_candidate(
        _sample_signal(symbol="UK100m", setup_type="pullback", entry_quality=99, confidence=80, within_reach=True, distance_atr=0.0),
        feat,
        config,
        recent_trades=[],  # thin recent sample -> global edge gate applies
    )
    assert out["evaluation"]["action"] == "skip"
    assert "global_edge_cold" in out["evaluation"]["reason"]


def test_warm_symbol_is_not_blocked_by_recent_gate(growth_profile):
    """A profitable symbol with good recent trades still executes."""
    from core.utils import load_config

    config = load_config()
    config["evaluation"]["symbol_blocklist"] = []
    feat = {"price": 2400.0, "atr": 4.0, "volume_ratio": 1.2}
    warm = [{"symbol": "USOILm", "result": "win", "pnl": 0.5} for _ in range(10)]
    out = evaluate_candidate(
        _sample_signal(symbol="USOILm", entry_quality=80, confidence=75, within_reach=True, distance_atr=0.1),
        feat,
        config,
        recent_trades=warm,
    )
    assert out["evaluation"]["action"] == "execute"
