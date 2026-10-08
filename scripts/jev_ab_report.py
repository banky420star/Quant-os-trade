"""Compare stored JEV shadow decisions with closed Quant OS trades."""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from core.jev_ab_report import build_veto_report, match_shadow_to_trades
from core.utils import read_json_state, utc_now_iso, write_json_state

def main()->int:
    shadow=read_json_state("jev_decisions.json",default={}) or {}
    trade_log=read_json_state("trade_log.json",default={}) or {}
    decisions=list(shadow.get("decisions") or [])
    trades=list(trade_log.get("trades") or [])
    matches=match_shadow_to_trades(decisions,trades)
    report=build_veto_report(matches)
    report["generated_at"]=utc_now_iso()
    report["source_decisions"]=len(decisions)
    report["source_trades"]=len(trades)
    write_json_state("jev_ab_report.json",report)
    print(json.dumps(report,indent=2,default=str))
    return 0

if __name__=="__main__": raise SystemExit(main())
