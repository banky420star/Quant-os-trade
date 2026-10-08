"""Run one execution-free JEV shadow pass against current Quant OS state.

Writes state/jev_decisions.json only. Does not import or call execution/broker modules.
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from core.jev_decision_bus import JevDecisionBus, normalize_candidate
from core.utils import load_config, read_json_state, utc_now_iso, write_json_state

def _candidates()->list[dict[str,Any]]:
    evaluated=read_json_state("evaluated_signals.json",default={}) or {}
    rows=list(evaluated.get("evaluated") or [])
    if rows: return rows
    raw=read_json_state("candidate_signals.json",default={}) or {}
    return list(raw.get("candidates") or [])

def main()->int:
    config=load_config()
    jev_cfg=config.setdefault("jev",{})
    jev_cfg["enabled"]=True
    jev_cfg["mode"]="shadow"
    bus=JevDecisionBus(config)
    features=read_json_state("features.json",default={}) or {}
    market_context=read_json_state("market_context.json",default={}) or {}
    positions_doc=read_json_state("paper_positions.json",default={}) or {}
    risk_state=read_json_state("risk_state.json",default={}) or {}
    edge_scores=read_json_state("edge_scores.json",default={}) or {}
    trade_log=read_json_state("trade_log.json",default={}) or {}
    trades=list(trade_log.get("trades") or [])
    rows=[]
    now=utc_now_iso()
    for candidate in _candidates():
        state=normalize_candidate(candidate,features,market_context,
            list(positions_doc.get("positions") or []),
            risk_state=risk_state,edge_scores=edge_scores,recent_trades=trades)
        d=bus.decide(state)
        cside=str(candidate.get("side") or "").upper()
        if cside=="LONG": cside="BUY"
        if cside=="SHORT": cside="SELL"
        rows.append({"timestamp":now,"symbol":candidate.get("symbol"),
            "signal_id":candidate.get("signal_id"),"candidate_side":cside,
            "candidate_setup":candidate.get("setup_type"),
            "candidate_confidence":candidate.get("confidence"),
            "state":state,"decision":d.as_dict(),"agreement":d.action==cside})
    doc={"timestamp":now,"mode":"shadow","provider":bus.provider,"model":bus.model,
         "count":len(rows),"agreement_count":sum(1 for r in rows if r["agreement"]),
         "wait_count":sum(1 for r in rows if r["decision"]["action"]=="WAIT"),
         "decisions":rows}
    write_json_state("jev_decisions.json",doc)
    print(f"JEV shadow: {doc['count']} candidates | agree={doc['agreement_count']} | wait={doc['wait_count']}")
    for row in rows:
        d=row["decision"]
        print(f"{row['symbol']} {row['candidate_side']} -> {d['action']} "
              f"p={d['confidence']:.3f} provider={d['provider']} {d['latency_ms']:.0f}ms")
    return 0

if __name__=="__main__": raise SystemExit(main())
