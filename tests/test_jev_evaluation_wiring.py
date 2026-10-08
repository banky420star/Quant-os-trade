"""Exercise the real Quant OS evaluation loop with the paper-only JEV gate."""
import copy
import logging

from core.jev_decision_bus import JevDecision
from core import adaptive_gates
from core import jev_shadow_runtime
from loops import evaluation_loop


class FixedBus:
    model = "test-jev"
    def __init__(self, _cfg):
        self.n = 0

    def decide(self, state):
        self.n += 1
        action = "BUY" if state["symbol"]=="XAUUSDm" else "WAIT"
        return JevDecision(action=action,
            probabilities={"BUY":0.9 if action=="BUY" else 0.0,
                           "SELL":0.0,"WAIT":0.1 if action=="BUY" else 1.0},
            confidence=0.9,provider="openrouter",model=self.model,latency_ms=1.0)


def test_evaluation_loop_uses_real_paper_gate(monkeypatch):
    config={"execution":{"mode":"paper","live_trading_enabled":False},
            "evaluation":{"enabled":True,"mode":"shadow"},
            "jev":{"enabled":True,"mode":"paper_gate","max_candidates_per_cycle":5,
                   "cache_seconds":0}}
    candidates=[
        {"symbol":"XAUUSDm","side":"BUY","signal_id":"buy1",
         "confidence":80,"setup_type":"trend"},
        {"symbol":"EURUSDm","side":"SELL","signal_id":"sell2",
         "confidence":80,"setup_type":"trend"},
    ]
    records={}
    def read_doc(name,default=None):
        mapping={
           "features.json":{"symbols":{"XAUUSDm":{"price":2600},"EURUSDm":{"price":1.1}}},
           "market_context.json":{"market_context":{"symbols":{}}},
           "paper_positions.json":{"positions":[]},
           "risk_state.json":{},
           "edge_scores.json":{},
           "paper_trades.json":{"trades":[]}
        }
        return mapping.get(name,default)
    monkeypatch.setattr(evaluation_loop,"load_config",lambda:copy.deepcopy(config))
    monkeypatch.setattr(evaluation_loop,"setup_logger",lambda *a:logging.getLogger("testjev"))
    monkeypatch.setattr(evaluation_loop,"candidates_available",lambda c:True)
    monkeypatch.setattr(evaluation_loop,"fail_safe_missing",lambda *a:False)
    monkeypatch.setattr(evaluation_loop,"read_candidate_signals",
                        lambda c:{"candidates":copy.deepcopy(candidates)})
    monkeypatch.setattr(evaluation_loop,"read_json_state",read_doc)
    monkeypatch.setattr(evaluation_loop,"evaluate_batch",
                        lambda rows,*a,**k:(copy.deepcopy(rows),[]))
    monkeypatch.setattr(evaluation_loop,"write_json_state",
                        lambda name,doc:records.__setitem__(name,doc))
    monkeypatch.setattr(evaluation_loop,"sync_store_from_doc",lambda *a:None)
    monkeypatch.setattr(evaluation_loop,"fast_mode_enabled",lambda c:False)
    monkeypatch.setattr(adaptive_gates,"compute_adaptive_gates",lambda c:{"enabled":False})
    monkeypatch.setattr(jev_shadow_runtime,"JevDecisionBus",FixedBus)
    original_evaluate=jev_shadow_runtime.evaluate_candidates
    monkeypatch.setattr(
        jev_shadow_runtime, "evaluate_candidates",
        lambda *args, **kwargs: original_evaluate(*args, **{**kwargs, "persist": False}),
    )
    out=evaluation_loop.run()
    assert out["count"]==1
    assert [r["signal_id"] for r in out["evaluated"]]==["buy1"]
    assert records["evaluated_signals.json"]["count"]==1


def test_evaluation_loop_does_not_gate_live_mt5(monkeypatch):
    config={"execution":{"mode":"mt5","live_trading_enabled":True},
            "evaluation":{"enabled":True,"mode":"shadow"},
            "jev":{"enabled":True,"mode":"paper_gate","max_candidates_per_cycle":1,
                   "cache_seconds":0}}
    assert jev_shadow_runtime._effective_mode(config)=="shadow"
