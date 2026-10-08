"""JEV paper-gate demonstration on crafted Quant OS signals (no live orders).

Uses the repository's existing signal generator, verifier, and PaperBroker.
The market snapshot is synthetic and results DO NOT estimate real performance.
"""
from __future__ import annotations

import copy, json, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from core.utils import load_config
from core.signal_engine import SignalEngine
from core.verifier import Verifier
from core.paper_broker import PaperBroker
from core.jev_shadow_runtime import evaluate_candidates

def main()->int:
    cfg=load_config()
    assert cfg.get("execution",{}).get("mode")=="paper", "Refusing non-paper mode"
    assert cfg.get("execution",{}).get("live_trading_enabled") is False, "Refusing live-trading flag"
    assert cfg.get("jev",{}).get("mode")=="paper_gate", "Refusing non-paper gate mode"
    features={"timestamp":"2026-10-09T00:00:00+00:00","symbols":{
      "XAUUSDm":{
        "symbol":"XAUUSDm","price":2650.0,"close":2650.0,
        "m5_trend":"bearish","m15_trend":"bearish",
        "bb_upper":2660.0,"bb_middle":2650.0,"bb_lower":2640.0,
        "bb_position":0.85,"stoch_k":78.0,"stoch_d":72.0,
        "stoch_cross":"bearish_cross","atr":5.0,"atr_ratio":0.0019,
        "volume_avg":1000,"volume_ratio":2.0,"support":2640.0,
        "resistance":2660.0,"rejection":"bearish_rejection",
        "breakout":"none","timeframe_alignment":True,
        "volatility_regime":"normal","spread_points":20.0,
      }
    }}
    ctx={"market_context":{"symbols":{"XAUUSDm":{
       "session":"london","regime":"trend","bias":"bearish","trend":"down"
    }}}}
    candidates=SignalEngine(cfg).generate_candidates(features,{"setups":{}})
    print("generated_candidates=",len(candidates))
    if not candidates:
        print("No synthetic candidates. Nothing submitted to paper broker.")
        return 1
    kept,audit=evaluate_candidates(candidates,cfg,features=features,
                                    market_context=ctx,
                                    risk_state={"kill_switch":False},
                                    persist=False)
    verifier=Verifier(cfg)
    baseline_approved,baseline_rejected=verifier.verify_batch(
        candidates,features,spread_data={"XAUUSDm":20.0})
    gated_approved,gated_rejected=verifier.verify_batch(
        kept,features,spread_data={"XAUUSDm":20.0})
    paper=PaperBroker(cfg)
    default_balance={"cash":100.0,"equity":100.0,"starting_cash":100.0}
    base_result=paper.process_approved_signals(
        copy.deepcopy(baseline_approved),{"XAUUSDm":2650.0},balance_state=copy.deepcopy(default_balance))
    gated_result=paper.process_approved_signals(
        copy.deepcopy(gated_approved),{"XAUUSDm":2650.0},balance_state=copy.deepcopy(default_balance))
    summary={
      "fixture":"synthetic Quant OS XAUUSD",
      "broker":"PaperBroker only",
      "candidate_count":len(candidates),
      "baseline_verified":len(baseline_approved),
      "jev_paper_gate_verified":len(gated_approved),
      "baseline_paper_positions":len(base_result.get("positions",[])),
      "jev_paper_positions":len(gated_result.get("positions",[])),
      "jev_real_provider_count":audit["real_provider_count"],
      "jev_vetoed_count":audit["vetoed_count"],
      "verifier_rejections":[
          {"rejection_reason":r.get("rejection_reason"),
           "failure_codes":r.get("failure_codes"),
           "reason":r.get("reason")}
          for r in baseline_rejected[:3]
      ],
      "decisions":[{"symbol":r["symbol"],"candidate":r["candidate_side"],
          "jev":r["decision"]["action"],"probabilities":r["decision"]["probabilities"],
          "provider":r["decision"]["provider"],"latency_ms":r["decision"]["latency_ms"]}
           for r in audit["decisions"]],
    }
    print(json.dumps(summary,indent=2))
    return 0

if __name__=="__main__": raise SystemExit(main())
