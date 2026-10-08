"""Score real historical Quant OS trades against archived JEV SHADOW calls.

Rejects paper-gated histories as invalid for baseline-veto estimation: vetoed
trades never happened, so their returns cannot be inferred after the fact.
"""
from __future__ import annotations

import json, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from core.jev_ab_report import build_veto_report, match_shadow_to_trades
from core.utils import STATE_DIR, read_json_state, utc_now_iso, write_json_state


def _read_archive(path:Path)->list[dict]:
    if not path.exists(): return []
    out=[]
    for line in path.read_text(encoding="utf-8",errors="replace").splitlines():
        try: row=json.loads(line)
        except json.JSONDecodeError: continue
        if isinstance(row,dict): out.append(row)
    return out


def main()->int:
    archive=_read_archive(STATE_DIR/"jev_decision_archive.jsonl")
    decisions=[r for r in archive
        if r.get("mode")=="shadow" and
        (r.get("decision") or {}).get("provider")=="openrouter"]
    gated=[r for r in archive if r.get("mode")=="paper_gate"]
    trade_log=read_json_state("trade_log.json",default={}) or {}
    trades=[
        t for t in list(trade_log.get("trades") or [])
        if isinstance(t,dict) and not t.get("archive_polluted")
    ]
    matches=match_shadow_to_trades(decisions,trades)
    report={
        "generated_at":utc_now_iso(),
        "source_archive_rows":len(archive),
        "real_shadow_decisions":len(decisions),
        "paper_gate_rows_excluded":len(gated),
        "closed_trade_records":len(trades),
        "matched_trades":len(matches),
    }
    if not matches:
        report.update({
            "status":"insufficient_matched_outcomes",
            "note":"Cannot calculate a credible JEV uplift: no matched baseline trades with genuine JEV shadow decisions and closed P&L. Paper-gate vetoes are excluded because the unfilled counterfactual outcome is unknown.",
        })
    else:
        report.update(build_veto_report(matches))
        report["status"]="observational_shadow_comparison"
        report["note"]="Subset/veto comparison is not a causal RCT. Fees, slippage, execution behavior and overlapping portfolio exposure can change the results."
    write_json_state("jev_ab_report.json",report)
    print(json.dumps(report,indent=2,default=str))
    return 0

if __name__=="__main__": raise SystemExit(main())
