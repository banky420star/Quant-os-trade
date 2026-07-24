"""Market-adaptive regime evolution — map, promote/demote, verify path."""

from __future__ import annotations

import copy

import pytest

from core.regime_evolution import (
    base_regime_policy,
    effective_regime_settings,
    evolve_from_trades,
    normalize_regime,
    refresh_regime_evolution,
    regime_cell_key,
)
from core.verifier import Verifier


def _cfg(**signals_extra):
    signals = {
        "min_confidence": 50,
        "min_risk_reward": 1.0,
        "regime_overrides": {},
    }
    signals.update(signals_extra)
    return {
        "signals": signals,
        "filters": {
            "min_volume_ratio": 0.0,
            "spread_mult": 2.0,
            "min_atr_ratio": 0.0,
            "avoid_news": False,
            "max_spread_points": {},
        },
        "trading": {"aggressive_mode": True},
        "execution": {"starting_cash": 1000},
        "risk": {},
        "adaptation": {
            "regime_evolution": {
                "enabled": True,
                "min_n": 6,
                "promote_exp": 0.05,
                "demote_exp": 0.0,
            }
        },
        "culturing": {"min_n": 6},
        "practice": {},
        "intelligence": {},
    }


def test_normalize_regime_aliases():
    assert normalize_regime("trending") == "weak_trend"
    assert normalize_regime("ranging") == "range"
    assert normalize_regime("strong_trend") == "strong_trend"
    assert normalize_regime("vol_spike") == "volatility_spike"
    assert normalize_regime(None) == "unknown"


def test_base_map_distinct_per_regime():
    """Acceptance 1: different regimes → different setup bias / gate deltas."""
    trend = base_regime_policy("strong_trend")
    rng = base_regime_policy("range")
    spike = base_regime_policy("volatility_spike")
    assert "trend_continuation" in trend["preferred_setups"]
    assert "mean_reversion" in rng["preferred_setups"]
    assert trend["min_confidence_delta"] != rng["min_confidence_delta"]
    assert spike["skip"] is True
    assert trend["skip"] is False


def test_effective_settings_differ_by_regime_same_fixture():
    """Same symbol/setup, two regimes → distinct effective conf/rr."""
    cfg = _cfg()
    a = effective_regime_settings(cfg, symbol="EURUSDm", regime="strong_trend", setup="pullback")
    b = effective_regime_settings(cfg, symbol="EURUSDm", regime="range", setup="pullback")
    c = effective_regime_settings(cfg, symbol="EURUSDm", regime="volatility_spike", setup="pullback")
    assert a["regime"] == "strong_trend"
    assert b["regime"] == "range"
    assert a["min_confidence"] != b["min_confidence"] or a["min_risk_reward"] != b["min_risk_reward"]
    assert c["skip"] is True
    assert a["skip"] is False
    # pullback preferred in both trend and range base maps
    assert a["setup_preferred"] is True
    assert b["setup_preferred"] is True
    # trend_continuation preferred in trend, demoted in range
    t_tc = effective_regime_settings(
        cfg, symbol="EURUSDm", regime="strong_trend", setup="trend_continuation"
    )
    r_tc = effective_regime_settings(
        cfg, symbol="EURUSDm", regime="range", setup="trend_continuation"
    )
    assert t_tc["setup_preferred"] is True
    assert r_tc["setup_demoted"] is True
    assert t_tc["min_confidence"] < r_tc["min_confidence"]


def _trade(symbol, regime, setup, r_multiple):
    return {
        "symbol": symbol,
        "regime_primary": regime,
        "setup": setup,
        "r_multiple": r_multiple,
        "result": "win" if r_multiple > 0 else "loss",
    }


def test_evolve_promotes_and_demotes_from_trades():
    """Acceptance 2: closed trades → promote/demote cells with min_n floor."""
    trades = []
    # Good cell: EURUSD | strong_trend | pullback
    for _ in range(8):
        trades.append(_trade("EURUSDm", "strong_trend", "pullback", 0.4))
    # Bad cell: EURUSD | range | trend_continuation
    for _ in range(8):
        trades.append(_trade("EURUSDm", "range", "trend_continuation", -0.5))
    # Thin cell: not eligible
    for _ in range(3):
        trades.append(_trade("EURUSDm", "compression", "breakout", 1.0))

    state = evolve_from_trades(trades, min_n=6, promote_exp=0.05, demote_exp=0.0)
    good_key = regime_cell_key("EURUSDm", "strong_trend", "pullback")
    bad_key = regime_cell_key("EURUSDm", "range", "trend_continuation")
    thin_key = regime_cell_key("EURUSDm", "compression", "breakout")

    assert state["cells"][good_key]["action"] == "promote"
    assert state["cells"][good_key]["eligible"] is True
    assert state["cells"][bad_key]["action"] == "demote"
    assert state["cells"][thin_key]["action"] == "hold"
    assert state["cells"][thin_key]["eligible"] is False
    assert state["n_promoted"] >= 1
    assert state["n_demoted"] >= 1

    pref = state["preferred_setups_by_regime"].get("EURUSDm|strong_trend") or []
    dem = state["demoted_setups_by_regime"].get("EURUSDm|range") or []
    assert "pullback" in pref
    assert "trend_continuation" in dem


def test_refresh_writes_durable_state(tmp_path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    trades = [_trade("XAUUSDm", "strong_trend", "pullback", 0.3) for _ in range(8)]
    utils.write_json_state("trade_log.json", {"trades": trades})

    out = refresh_regime_evolution(_cfg(), trades=None)
    assert out["n_cells"] >= 1
    saved = utils.read_json_state("regime_evolution.json")
    assert saved is not None
    assert saved.get("n_cells") == out["n_cells"]
    assert saved.get("cells")


def test_effective_settings_use_evolved_deltas_once():
    """Promote/demote conf delta applied once (setup cell preferred over aggregate)."""
    trades = [_trade("EURUSDm", "strong_trend", "pullback", 0.5) for _ in range(10)]
    state = evolve_from_trades(trades, min_n=6)
    cfg = _cfg()
    sk = regime_cell_key("EURUSDm", "strong_trend", "pullback")
    rk = regime_cell_key("EURUSDm", "strong_trend")
    assert state["cells"][sk]["action"] == "promote"
    assert state["cells"][rk]["action"] == "promote"
    assert state["cells"][sk]["min_confidence_delta"] == -4
    assert state["cells"][rk]["min_confidence_delta"] == -4

    base = effective_regime_settings(
        cfg, symbol="EURUSDm", regime="strong_trend", setup="pullback", state={}
    )
    evolved = effective_regime_settings(
        cfg, symbol="EURUSDm", regime="strong_trend", setup="pullback", state=state
    )
    assert evolved["source"] == "evolved"
    # base: 50 + strong_trend(-5) + preferred(-3) = 42
    # evolved once: 42 + promote(-4) = 38  — not double-applied 34
    assert base["min_confidence"] == pytest.approx(42.0)
    assert evolved["min_confidence"] == pytest.approx(38.0)
    assert evolved["min_confidence"] == base["min_confidence"] - 4.0


def test_verifier_regime_switch_changes_gates(tmp_path, monkeypatch):
    """Acceptance 3: verify path applies different gates when regime changes."""
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)
    utils.write_json_state("symbol_policy_live.json", {})
    utils.write_json_state("regime_evolution.json", {})

    cfg = _cfg()
    cfg["signals"]["min_confidence"] = 50
    v = Verifier(cfg)

    def _sig(regime: str, setup: str = "pullback", conf: float = 52.0):
        return {
            "signal_id": f"t-{regime}-{setup}-{conf}",
            "symbol": "EURUSDm",
            "side": "BUY",
            "setup_type": setup,
            "confidence": conf,
            "entry": 1.1000,
            "sl": 1.0980,
            "tp1": 1.1040,
            "tp2": 1.1060,
            "market_context": {
                "session": "london_open",
                "market_regime": {"primary": regime, "bias": "bullish"},
            },
        }

    feat = {
        "atr_ratio": 0.01,
        "volume_ratio": 1.0,
        "m5_trend": "bullish",
        "m15_trend": "bullish",
        "h1_trend": "bullish",
        "bb_position": 0.4,
    }

    # Volatility spike hard-rejects; preferred pullback in strong_trend can approve.
    spike = v._verify_one(_sig("volatility_spike", conf=90), feat, [], False, 0.0, 1000.0, [])
    trend = v._verify_one(_sig("strong_trend", conf=52), feat, [], False, 0.0, 1000.0, [])
    assert spike["checks"].get("regime_allowed") is False
    assert spike["approved"] is False
    assert "regime_allowed" in (spike.get("failure_codes") or [])
    assert trend["checks"].get("regime_allowed") is True
    assert trend["approved"] is True, f"expected approve, failures={trend.get('failures')} codes={trend.get('failure_codes')}"
    # Soft preferred/demoted must not appear as hard failure codes
    assert "regime_setup_preferred" not in (trend.get("failure_codes") or [])
    assert "regime_setup_demoted" not in (trend.get("failure_codes") or [])
    assert "regime_setup_preferred" not in (trend.get("checks") or {})
    assert "regime_setup_demoted" not in (trend.get("checks") or {})

    t_cont = _sig("strong_trend", setup="trend_continuation", conf=55)
    r_cont = _sig("range", setup="trend_continuation", conf=55)
    vt = v._verify_one(t_cont, feat, [], False, 0.0, 1000.0, [])
    vr = v._verify_one(r_cont, feat, [], False, 0.0, 1000.0, [])
    re_t = t_cont.get("regime_evolution") or {}
    re_r = r_cont.get("regime_evolution") or {}
    assert float(re_t["min_confidence"]) < float(re_r["min_confidence"])
    assert re_r.get("setup_demoted") is True
    assert re_t.get("setup_preferred") is True
    # Demoted stamp alone must not reject via soft-flag check names
    assert "regime_setup_demoted" not in (vr.get("failure_codes") or [])
    assert "regime_setup_preferred" not in (vt.get("failure_codes") or [])
    # Preferred setup path still approves at same conf when gates pass
    assert vt["approved"] is True, f"preferred failed: {vt.get('failure_codes')}"


def test_verifier_reads_populated_evolved_state(tmp_path, monkeypatch):
    """Criterion 3: Verifier loads evolved state and regime switch still differs."""
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)
    utils.write_json_state("symbol_policy_live.json", {})

    # Promote pullback in strong_trend; demote trend_continuation in range
    trades = (
        [_trade("EURUSDm", "strong_trend", "pullback", 0.5) for _ in range(10)]
        + [_trade("EURUSDm", "range", "trend_continuation", -0.6) for _ in range(10)]
    )
    state = evolve_from_trades(trades, min_n=6)
    utils.write_json_state("regime_evolution.json", state)

    cfg = _cfg()
    cfg["signals"]["min_confidence"] = 50
    v = Verifier(cfg)
    assert v._regime_evolution_on is True
    assert (v._regime_evolution or {}).get("cells")

    def _sig(regime: str, setup: str, conf: float):
        return {
            "signal_id": f"e-{regime}-{setup}-{conf}",
            "symbol": "EURUSDm",
            "side": "BUY",
            "setup_type": setup,
            "confidence": conf,
            "entry": 1.1000,
            "sl": 1.0980,
            "tp1": 1.1040,
            "tp2": 1.1060,
            "market_context": {
                "session": "london_open",
                "market_regime": {"primary": regime, "bias": "bullish"},
            },
        }

    feat = {
        "atr_ratio": 0.01,
        "volume_ratio": 1.0,
        "m5_trend": "bullish",
        "m15_trend": "bullish",
        "h1_trend": "bullish",
        "bb_position": 0.4,
    }

    # conf between base map and demoted gate: may pass promote, fail demote
    prom = _sig("strong_trend", "pullback", 40)
    dem = _sig("range", "trend_continuation", 40)
    rp = v._verify_one(prom, feat, [], False, 0.0, 1000.0, [])
    rd = v._verify_one(dem, feat, [], False, 0.0, 1000.0, [])

    re_p = prom.get("regime_evolution") or {}
    re_d = dem.get("regime_evolution") or {}
    assert re_p.get("source") == "evolved"
    assert re_d.get("source") == "evolved"
    assert float(re_p["min_confidence"]) < float(re_d["min_confidence"])
    # Prefer path: conf 40 meets evolved promote gate (~38)
    assert rp["checks"]["confidence"] is True
    assert rp["approved"] is True, f"promoted cell reject: {rp.get('failure_codes')}"
    # Demoted path: conf 40 below elevated demote gate
    assert rd["checks"]["confidence"] is False
    assert rd["approved"] is False
    assert "confidence" in (rd.get("failure_codes") or [])
    # Soft demote flags never appear as failure codes
    assert "regime_setup_demoted" not in (rd.get("failure_codes") or [])
    assert "regime_setup_preferred" not in (rp.get("failure_codes") or [])


def test_evaluation_policy_regime_skip_and_prefer(tmp_path, monkeypatch):
    from core import utils
    from core.evaluation_policy import evaluate_candidate

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)
    utils.write_json_state("edge_scores.json", {})
    utils.write_json_state("best_policies.json", {})
    utils.write_json_state("regime_evolution.json", {})

    cfg = {
        "evaluation": {
            "enabled": True,
            "mode": "live",
            "min_policy_score": 30,
            "skip_below_score": 20,
            "symbol_blocklist": [],
            "setup_blocklist": [],
        },
        "signals": {"min_confidence": 50, "min_risk_reward": 1.0},
        "adaptation": {"regime_evolution": {"enabled": True, "min_n": 6}},
        "trading": {},
    }
    feat = {
        "atr_ratio": 0.001,
        "volume_ratio": 1.2,
        "spread_points": 5,
        "bb_position": 0.5,
        "m5_trend": "bullish",
    }

    def _cand(regime, setup="pullback"):
        return {
            "symbol": "EURUSDm",
            "side": "BUY",
            "setup_type": setup,
            "confidence": 70,
            "distance_atr": 0.2,
            "within_reach": True,
            "market_context": {
                "session": "london_open",
                "market_regime": {"primary": regime, "bias": "bullish"},
            },
        }

    skip_row = evaluate_candidate(_cand("volatility_spike"), feat, cfg, recent_trades=[])
    ok_row = evaluate_candidate(_cand("strong_trend"), feat, cfg, recent_trades=[])
    assert skip_row["evaluation"]["action"] == "skip"
    assert "regime_skip" in (skip_row["evaluation"]["reason"] or "")
    assert ok_row["evaluation"].get("regime") == "strong_trend"
    # Prefer vs demote score path for trend_continuation
    pref = evaluate_candidate(
        _cand("strong_trend", "trend_continuation"), feat, cfg, recent_trades=[]
    )
    dem = evaluate_candidate(
        _cand("range", "trend_continuation"), feat, cfg, recent_trades=[]
    )
    assert pref["evaluation"]["policy_score"] > dem["evaluation"]["policy_score"]
