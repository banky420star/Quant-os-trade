"""Regression tests for paper-only shadow experiment arm routing."""

from __future__ import annotations

import copy

from core.paper_broker import PaperBroker
from core.shadow_experiment import (
    annotate_experiment_arm_signals,
    generate_experiments,
)
from core.utils import load_config


def _signal(signal_id: str, *, confidence: float = 90.0) -> dict:
    return {
        "signal_id": signal_id,
        "symbol": "XAUUSDm",
        "side": "BUY",
        "entry": 100.0,
        "sl": 95.0,
        "tp1": 110.0,
        "tp2": 115.0,
        "confidence": confidence,
        "risk_reward": 2.0,
        "setup_type": "test_setup",
    }


def test_routing_is_paper_only_and_deterministic(monkeypatch):
    cfg = {
        "execution": {"mode": "paper"},
        "signals": {"min_confidence": 40, "min_risk_reward": 1.0},
        "self_learning": {
            "shadow_experiments": {
                "routing_enabled": True,
                "control_fraction": 0.5,
            }
        },
    }
    experiments = generate_experiments(config=cfg)
    monkeypatch.setattr(
        "core.shadow_experiment.read_json_state",
        lambda *_args, **_kwargs: {"experiments": experiments},
    )

    records = [{"signal": _signal(f"sig-{i}")} for i in range(200)]
    first = annotate_experiment_arm_signals(records, cfg)
    second = annotate_experiment_arm_signals(records, cfg)

    first_arms = [r["signal"].get("experiment_arm") for r in first]
    second_arms = [r["signal"].get("experiment_arm") for r in second]
    assert first_arms == second_arms
    assert any(arm for arm in first_arms)
    assert any(arm is None for arm in first_arms)
    # The configured 50/50 split is approximate over a finite deterministic
    # sample; both sides must remain materially represented.
    routed = sum(bool(arm) for arm in first_arms)
    assert 60 < routed < 140

    mt5_cfg = copy.deepcopy(cfg)
    mt5_cfg["execution"]["mode"] = "mt5"
    assert annotate_experiment_arm_signals(records, mt5_cfg) is records


def test_experiment_arm_survives_order_position_and_closed_trade():
    cfg = copy.deepcopy(load_config())
    cfg["execution"]["mode"] = "paper"
    cfg.setdefault("trading", {}).setdefault("break_even", {})["enabled"] = False
    cfg.setdefault("trading", {}).setdefault("trailing", {})["enabled"] = False
    cfg.setdefault("trading", {}).setdefault("exits", {}).setdefault("partial_tp", {})["enabled"] = False
    broker = PaperBroker(cfg)
    signal = _signal("arm-lifecycle", confidence=95.0)
    signal["experiment_arm"] = "gate_conf_up_test"

    order = broker._create_order(signal, 100.0)
    assert order["experiment_arm"] == "gate_conf_up_test"

    position, _, _ = broker._fill_order(order, 100.0, 1.0)
    assert position["experiment_arm"] == "gate_conf_up_test"

    closed, trades, _ = broker._check_exits(
        [position],
        {"XAUUSDm": 95.0},
    )
    assert len(closed) == 1
    assert trades[-1]["experiment_arm"] == "gate_conf_up_test"


def test_rejected_order_keeps_experiment_arm():
    cfg = copy.deepcopy(load_config())
    cfg["execution"]["mode"] = "paper"
    broker = PaperBroker(cfg)
    signal = _signal("arm-rejected")
    signal["experiment_arm"] = "gate_test"

    order = broker._create_rejected_order(signal, 100.0, "test_rejection")
    assert order["experiment_arm"] == "gate_test"


def test_weight_proposals_are_not_routed_as_experiments(monkeypatch):
    cfg = {
        "execution": {"mode": "paper"},
        "signals": {"min_confidence": 40, "min_risk_reward": 1.0},
        "self_learning": {"shadow_experiments": {"routing_enabled": True}},
    }
    experiments = [{"id": "weight-only", "family": "weight", "patch": {"weights.foo": 1.0}}]
    monkeypatch.setattr(
        "core.shadow_experiment.read_json_state",
        lambda *_args, **_kwargs: {"experiments": experiments},
    )
    records = [{"signal": _signal("weight-only-signal")}]
    assert annotate_experiment_arm_signals(records, cfg) == records
