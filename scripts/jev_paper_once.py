"""Run one paper-only Quant OS JEV cycle on existing FRESH market state.

Requires the feed producer to have already written fresh features and
candidate signals. Does not start MT5 or fetch/synthesize market data.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from core.utils import load_config, read_json_state


def _seconds_old(value:object)->float:
    if not value: return float("inf")
    try:
        dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc)-dt).total_seconds()
    except (TypeError,ValueError):
        return float("inf")


def _assert_paper(config:dict)->None:
    ex=config.get("execution") or {}
    j=config.get("jev") or {}
    if (ex.get("mode")!="paper" or ex.get("live_trading_enabled") is not False
            or j.get("mode")!="paper_gate" or not j.get("enabled")):
        raise RuntimeError(
            "Refusing to run: execution.mode must be paper, live_trading_enabled "
            "must be false, and jev.mode must be enabled paper_gate."
        )


def preflight(config:dict)->dict:
    _assert_paper(config)
    docs={}
    for name in ("features.json","candidate_signals.json"):
        doc=read_json_state(name,default={}) or {}
        if not doc:
            raise RuntimeError(
                f"Missing {name}: first produce real Quant market features and "
                "candidate signals. Do not substitute synthetic demo data."
            )
        docs[name]=doc
    max_age=max(15,int((config.get("jev") or {}).get("max_input_age_seconds",180)))
    for name,doc in docs.items():
        age=_seconds_old(doc.get("timestamp"))
        if not 0<=age<=max_age:
            raise RuntimeError(
                f"Refusing stale {name}: age={age:.0f}s; limit={max_age}s."
            )
    count=len(docs["candidate_signals.json"].get("candidates") or [])
    return {"candidate_count":count,"max_input_age_seconds":max_age}


def main()->int:
    config=load_config()
    try:
        status=preflight(config)
    except RuntimeError as exc:
        print(f"JEV paper preflight BLOCKED: {exc}",file=sys.stderr)
        return 2
    from loops import evaluation_loop, verifier_loop, execution_loop
    existing=read_json_state("paper_orders.json",default={}) or {}
    prior_orders=len(existing.get("orders") or [])
    ev=evaluation_loop.run() or {}
    ver=verifier_loop.run() or {}
    exe=execution_loop.run() or {}
    audit=read_json_state("jev_decisions.json",default={}) or {}
    after=read_json_state("paper_orders.json",default={}) or {}
    new_orders=max(0,len(after.get("orders") or [])-prior_orders)
    print(json.dumps({
        "mode":"paper","candidate_count":status["candidate_count"],
        "evaluated":ev.get("count"),"jev_model_votes":audit.get("count"),
        "jev_real_provider_count":audit.get("real_provider_count"),
        "jev_paper_vetoes":audit.get("vetoed_count"),
        "verifier_approved":len(ver.get("approved") or []),
        "paper_orders_created":new_orders,
        "execution_result_mode":exe.get("mode"),
    },indent=2,default=str))
    return 0

if __name__=="__main__": raise SystemExit(main())
