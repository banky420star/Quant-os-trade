from __future__ import annotations

from core import adaptive_symbol_learner as learner


def _trade(symbol: str, r: float, confidence: float, hold: float = 120.0) -> dict:
    return {
        "symbol": symbol,
        "r_multiple": r,
        "result": "win" if r > 0 else "loss",
        "hold_seconds": hold,
        "confidence": confidence,
        "signal_meta": {"confidence": confidence},
    }


def _config() -> dict:
    return {
        "execution": {"mode": "paper"},
        "signals": {"min_confidence": 60},
        "trading": {"allow_pyramiding": True},
        "self_learning": {
            "adaptive_symbol_learning": {
                "enabled": True,
                "mode": "shadow",
                "min_trades": 4,
                "max_risk_multiplier_up": 1.10,
                "max_risk_multiplier_down": 0.50,
            }
        },
    }


def test_proposals_are_per_symbol_and_bounded():
    trades = (
        [_trade("XAUUSDm", 0.8, 80, 180)] * 4
        + [_trade("EURUSDm", -1.0, 80, 180)] * 4
    )
    out = learner.build_symbol_proposals(_config(), trades, {
        "symbols": {
            "XAUUSDm": {"atr": 2, "point": 0.01, "spread_points": 5, "volume_ratio": 1.0},
            "EURUSDm": {"atr": 0.01, "point": 0.0001, "spread_points": 2, "volume_ratio": 1.0},
        }
    })
    xau = out["symbols"]["XAUUSDm"]["proposed"]
    eur = out["symbols"]["EURUSDm"]["proposed"]
    assert 1.0 <= xau["risk_multiplier"] <= 1.10
    assert 0.50 <= eur["risk_multiplier"] <= 1.0
    assert xau["max_pyramid_layers"] == 1
    assert eur["max_pyramid_layers"] == 0
    assert out["source_ledger"] == "paper_trades.json"


def test_thin_samples_never_change_risk_or_pyramid():
    out = learner.build_symbol_proposals(
        _config(),
        [_trade("XAUUSDm", 2.0, 99)],
        {"symbols": {"XAUUSDm": {"atr": 2, "point": 0.01, "spread_points": 1}}},
    )
    row = out["symbols"]["XAUUSDm"]
    assert row["status"] == "insufficient_data"
    assert row["proposed"]["risk_multiplier"] == 1.0
    assert row["proposed"]["max_pyramid_layers"] == 0


def test_tick_observation_is_throttled_and_persisted(monkeypatch):
    saved = {}
    monkeypatch.setattr(learner, "read_json_state", lambda name, default=None: saved.get(name, default))
    monkeypatch.setattr(learner, "write_json_state", lambda name, doc: saved.__setitem__(name, doc))
    learner._LAST_TICK_WRITE = 0.0
    cfg = _config()
    cfg["self_learning"]["adaptive_symbol_learning"]["tick_sample_seconds"] = 5
    prices = {"XAUUSDm": {"mid": 2000.0, "spread_points": 4}}
    features = {"symbols": {"XAUUSDm": {"price": 2000.0, "atr": 2.0, "point": 0.01, "volume_ratio": 1.1}}}
    first = learner.record_tick_snapshot(cfg, prices, features, now=10.0)
    second = learner.record_tick_snapshot(cfg, prices, features, now=12.0)
    assert first is not None
    assert second is None
    assert saved[learner.TICK_STATE_FILE]["symbols"]["XAUUSDm"]["tick_count"] == 1


def test_live_mode_still_only_emits_shadow_proposals():
    cfg = _config()
    cfg["execution"]["mode"] = "mt5"
    out = learner.build_symbol_proposals(cfg, [_trade("XAUUSDm", 0.5, 80)] * 4)
    for row in out["symbols"].values():
        assert row["safety"]["live_config_changed"] is False
        assert row["safety"]["orders_placed"] == 0
