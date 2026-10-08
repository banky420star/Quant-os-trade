"""JEV is allowed to veto PAPER candidates, never live MT5 candidates."""
from __future__ import annotations

from core.jev_decision_bus import JevDecision
from core.jev_shadow_runtime import _effective_mode, evaluate_candidates


class FakeBus:
    model = "test-jev"
    def __init__(self, actions, provider="openrouter"):
        self.actions = list(actions)
        self.provider = provider
        self.calls = 0

    def decide(self, state):
        action = self.actions[self.calls]
        self.calls += 1
        return JevDecision(
            action=action,
            probabilities={"BUY": .75 if action=="BUY" else .0,
                           "SELL": .75 if action=="SELL" else .0,
                           "WAIT": .75 if action=="WAIT" else 0.0},
            confidence=.75,
            provider=self.provider,
            model=self.model,
            latency_ms=30.0,
        )


def cfg(mode="paper", live=False, jev_mode="paper_gate"):
    return {"execution":{"mode":mode,"live_trading_enabled":live},
            "jev":{"enabled":True,"mode":jev_mode,
                   "max_candidates_per_cycle":10,"cache_seconds":0}}


def candidates():
    return [{"signal_id":"long-1","symbol":"XAUUSDm","side":"BUY",
             "confidence":78,"setup_type":"trend"},
            {"signal_id":"short-2","symbol":"EURUSDm","side":"SELL",
             "confidence":72,"setup_type":"reversal"},
            {"signal_id":"long-3","symbol":"BTCUSDm","side":"BUY",
             "confidence":55,"setup_type":"breakout"}]


def test_paper_gate_veto_and_does_not_modify_candidates():
    inputs=candidates()
    before=[dict(row) for row in inputs]
    kept,audit=evaluate_candidates(inputs,cfg(),
                 bus=FakeBus(["BUY","WAIT","SELL"]),persist=False)
    assert [r["signal_id"] for r in kept]==["long-1"]
    assert audit["mode"]=="paper_gate"
    assert audit["accepted_count"]==1
    assert audit["vetoed_count"]==2
    assert inputs==before


def test_gate_has_no_effect_on_live_mt5():
    inputs=candidates()
    kept,audit=evaluate_candidates(inputs,cfg("mt5",True),
                 bus=FakeBus(["WAIT","WAIT","WAIT"]),persist=False)
    assert kept==inputs
    assert audit["mode"]=="shadow"
    assert audit["accepted_count"]==3


def test_heuristic_never_approves_paper_gate():
    inputs=candidates()[:1]
    kept,audit=evaluate_candidates(inputs,cfg(),
                 bus=FakeBus(["BUY"],provider="heuristic"),persist=False)
    assert kept==[]
    assert audit["decisions"][0]["agreement"] is False


def test_shadow_mode_preserves_candidates():
    inputs=candidates()
    kept,audit=evaluate_candidates(inputs,cfg(jev_mode="shadow"),
                 bus=FakeBus(["WAIT","WAIT","WAIT"]),persist=False)
    assert kept==inputs and audit["vetoed_count"]==0


def test_gate_requires_live_trading_disabled():
    assert _effective_mode(cfg("paper",True))=="shadow"
    assert _effective_mode(cfg("mt5",False))=="shadow"


def test_inference_budget_does_not_accidentally_allow():
    inputs=candidates()
    c=cfg()
    c["jev"]["max_candidates_per_cycle"]=1
    bus=FakeBus(["BUY"])
    kept,audit=evaluate_candidates(inputs,c,bus=bus,persist=False)
    assert len(kept)==1 and bus.calls==1
    assert audit["decisions"][1]["decision"]["provider"]=="budget_exceeded"
