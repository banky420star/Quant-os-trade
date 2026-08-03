from __future__ import annotations

from core import adaptive_symbol_validation as validation


def _cfg() -> dict:
    return {
        "execution": {"mode": "paper"},
        "self_learning": {
            "adaptive_symbol_learning": {
                "validation": {
                    "enabled": True,
                    "min_forward_trades": 3,
                    "min_control_trades": 3,
                    "min_reward_margin": 0.30,
                    "min_expectancy_r": 0.0,
                }
            }
        },
    }


def _trade(symbol: str, r: float, proposal_id: str | None = None) -> dict:
    row = {"symbol": symbol, "r_multiple": r, "result": "win" if r > 0 else "loss"}
    if proposal_id:
        row["adaptive_symbol_proposal_id"] = proposal_id
    return row


def _proposal(symbol: str = "XAUUSDm") -> tuple[dict, str]:
    proposed = {
        "min_confidence": 65,
        "risk_multiplier": 1.05,
        "entry_delay_seconds": 0,
        "target_hold_seconds": 120,
        "max_pyramid_layers": 0,
    }
    pid = validation.proposal_id(symbol, proposed)
    return {"symbols": {symbol: {"status": "shadow_candidate", "proposed": proposed}}}, pid


def test_paper_router_tags_only_candidate_arm(monkeypatch):
    doc, pid = _proposal()
    monkeypatch.setattr(validation, "read_json_state", lambda name, default=None: doc if name == "adaptive_symbol_learning.json" else default)
    import hashlib
    ids = ["candidate-1", "sig-0"]
    ids.sort(key=lambda value: int(hashlib.sha1(value.encode("utf-8")).hexdigest()[:8], 16) % 2)
    records = [{"signal": {"signal_id": value, "symbol": "XAUUSDm", "confidence": 70}} for value in ids]
    routed = validation.annotate_paper_validation_signals(records, _cfg())
    tagged = [row["signal"] for row in routed if row["signal"].get("adaptive_symbol_proposal_id")]
    assert len(tagged) == 1
    assert tagged[0]["adaptive_symbol_proposal_id"] == pid
    assert tagged[0]["adaptive_validation_arm"] == "candidate"
    assert 0.0 < tagged[0]["conviction_size_mult"] <= 1.10


def test_paper_router_does_not_touch_mt5(monkeypatch):
    doc, _ = _proposal()
    monkeypatch.setattr(validation, "read_json_state", lambda name, default=None: doc if name == "adaptive_symbol_learning.json" else default)
    signal = {"signal": {"signal_id": "sig-0", "symbol": "XAUUSDm"}}
    cfg = _cfg()
    cfg["execution"]["mode"] = "mt5"
    assert validation.annotate_paper_validation_signals([signal], cfg) == [signal]


def test_waits_for_forward_paper_sample():
    doc, pid = _proposal()
    out = validation.validate_symbol_proposals(
        _cfg(), doc, [_trade("XAUUSDm", 0.5, pid), _trade("XAUUSDm", -0.2, pid)]
    )
    row = out["symbols"]["XAUUSDm"]
    assert row["status"] == "awaiting_forward_data"
    assert out["promotion_candidates"] == []
    assert row["safety"]["paper_only"] is True


def test_promotes_only_tagged_candidate_that_beats_same_symbol_control():
    doc, pid = _proposal()
    trades = [_trade("XAUUSDm", 0.8, pid)] * 3 + [_trade("XAUUSDm", -0.1)] * 3
    out = validation.validate_symbol_proposals(_cfg(), doc, trades)
    assert out["promotion_candidates"] == ["XAUUSDm"]
    assert out["symbols"]["XAUUSDm"]["status"] == "promotion_candidate"
    assert out["safety"]["live_config_changed"] is False


def test_live_mode_skips_paper_validation():
    doc, pid = _proposal()
    cfg = _cfg()
    cfg["execution"]["mode"] = "mt5"
    out = validation.validate_symbol_proposals(cfg, doc, [_trade("XAUUSDm", 0.8, pid)] * 3)
    assert out["status"] == "skipped_live_mode"
    assert out["promotion_candidates"] == []
    assert out["safety"]["paper_only"] is True


def test_generic_proposal_ids_do_not_count_as_adaptive_evidence():
    doc, pid = _proposal()
    trades = [_trade("XAUUSDm", 0.8, pid)] * 3
    trades += [{"symbol": "XAUUSDm", "r_multiple": 0.5, "proposal_id": pid}] * 3
    out = validation.validate_symbol_proposals(_cfg(), doc, trades)
    assert out["symbols"]["XAUUSDm"]["control"]["n"] == 3


def test_explicit_r_and_control_sample_gates_are_enforced():
    doc, pid = _proposal()
    trades = [_trade("XAUUSDm", 0.8, pid)] * 3
    trades += [{"symbol": "XAUUSDm", "pnl": 100, "result": "win"}] * 10
    out = validation.validate_symbol_proposals(_cfg(), doc, trades)
    row = out["symbols"]["XAUUSDm"]
    assert row["status"] == "awaiting_control_data"
    assert row["control"]["n"] == 0


def test_paper_broker_preserves_adaptive_tag():
    from core.paper_broker import PaperBroker
    cfg = {
        "execution": {"mode": "paper", "starting_cash": 1000},
        "signals": {"default_risk_percent": 1, "kelly_sizing": {"enabled": False}},
        "trading": {"allow_pyramiding": False},
        "risk": {"max_symbol_exposure_usd": 10000, "max_total_exposure_usd": 10000},
    }
    signal = {"signal_id": "tagged", "adaptive_symbol_proposal_id": "XAUUSDm_demo", "symbol": "XAUUSDm", "side": "BUY", "entry": 100.0, "sl": 95.0, "tp1": 110.0}
    result = PaperBroker(cfg).process_approved_signals([{"signal": signal}], {"XAUUSDm": 100.0})
    assert result["positions"][0]["adaptive_symbol_proposal_id"] == "XAUUSDm_demo"


def test_degradation_holds_and_clears_active_overrides(monkeypatch):
    doc, pid = _proposal()
    saved = {validation.ACTIVE_OVERRIDES_FILE: {"symbols": {"XAUUSDm": {"risk_multiplier": 1.05}}}}
    monkeypatch.setattr(validation, "read_json_state", lambda name, default=None: saved.get(name, default))
    writes = {}
    monkeypatch.setattr(validation, "write_json_state", lambda name, value: writes.__setitem__(name, value))
    out = validation.validate_symbol_proposals(
        _cfg(), doc, [_trade("XAUUSDm", 0.8, pid)] * 3, rollback_recommended=True
    )
    assert out["status"] == "rollback_hold"
    assert out["rolled_back"] is True
    assert writes[validation.ACTIVE_OVERRIDES_FILE]["symbols"] == {}
    assert out["symbols"]["XAUUSDm"]["status"] == "rollback_hold"
