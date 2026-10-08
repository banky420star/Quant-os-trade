"""Conservative A/B scoring for JEV shadow decisions.

Only a JEV veto is simulated. Opposite-side JEV opinions are not converted
into hypothetical trades because no counterfactual fill exists.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone
from typing import Any

def _float(v:Any,default:float=0.0)->float:
    try: x=float(v)
    except (TypeError,ValueError): return default
    return x if math.isfinite(x) else default

def _dt(value:Any)->datetime|None:
    if not value: return None
    try:
        d=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError,TypeError): return None

def normalize_side(side:Any)->str:
    s=str(side or "").upper()
    return "BUY" if s=="LONG" else "SELL" if s=="SHORT" else s

def metrics(trades:list[dict[str,Any]])->dict[str,Any]:
    pnls=[_float(t.get("pnl")) for t in trades]
    wins=sum(1 for x in pnls if x>0); losses=sum(1 for x in pnls if x<0)
    gross_win=sum(x for x in pnls if x>0); gross_loss=-sum(x for x in pnls if x<0)
    equity=peak=max_dd=0.0
    for pnl in pnls:
        equity+=pnl; peak=max(peak,equity); max_dd=max(max_dd,peak-equity)
    rs=[_float(t.get("r_multiple"),math.nan) for t in trades]
    rs=[x for x in rs if math.isfinite(x)]
    return {"trades":len(trades),"wins":wins,"losses":losses,
        "win_rate_pct":round(100.0*wins/max(wins+losses,1),2),
        "total_pnl":round(sum(pnls),4),"avg_pnl":round(sum(pnls)/max(len(pnls),1),4),
        "profit_factor":round(gross_win/gross_loss,4) if gross_loss>0 else (None if gross_win<=0 else float("inf")),
        "max_drawdown":round(max_dd,4),"avg_r":round(sum(rs)/len(rs),4) if rs else None}

def match_shadow_to_trades(shadow_rows:list[dict[str,Any]],trades:list[dict[str,Any]],*,max_seconds:int=900)->list[dict[str,Any]]:
    unused=set(range(len(trades))); matches=[]
    for row in shadow_rows:
        state=row.get("state") or {}; c=state.get("candidate") or {}
        signal_id=c.get("signal_id") or row.get("signal_id")
        symbol=str(row.get("symbol") or state.get("symbol") or "")
        side=normalize_side(row.get("candidate_side") or c.get("side"))
        decision_time=_dt(row.get("timestamp") or row.get("at")); selected=None
        if signal_id:
            for i in list(unused):
                t=trades[i]; trade_signal_id=t.get("signal_id") or ((t.get("conditions") or {}).get("signal_id"))
                if trade_signal_id and str(trade_signal_id)==str(signal_id): selected=i; break
        if selected is None:
            best_delta=None
            for i in list(unused):
                t=trades[i]
                if str(t.get("symbol") or "")!=symbol or normalize_side(t.get("side"))!=side: continue
                trade_time=_dt(t.get("opened_at") or t.get("closed_at"))
                if decision_time is None or trade_time is None: continue
                delta=abs((trade_time-decision_time).total_seconds())
                if delta<=max_seconds and (best_delta is None or delta<best_delta):
                    best_delta,selected=delta,i
        if selected is not None:
            unused.remove(selected); matches.append({"shadow":row,"trade":trades[selected]})
    return matches

def build_veto_report(matches:list[dict[str,Any]])->dict[str,Any]:
    baseline=[m["trade"] for m in matches]; kept=[]; vetoed=[]
    for m in matches:
        row,trade=m["shadow"],m["trade"]
        candidate_side=normalize_side(row.get("candidate_side") or ((row.get("state") or {}).get("candidate") or {}).get("side"))
        action=normalize_side((row.get("decision") or {}).get("action"))
        if action==candidate_side and action in {"BUY","SELL"}: kept.append(trade)
        else: vetoed.append(trade)
    base=metrics(baseline); shadow=metrics(kept)
    return {"matched_trades":len(matches),
        "coverage_note":"Only matched historical trades are scored; opposite-side counterfactual trades are never invented.",
        "baseline":base,"jev_veto_shadow":shadow,"vetoed_trades":len(vetoed),
        "avoided_losses":sum(1 for t in vetoed if _float(t.get("pnl"))<0),
        "skipped_winners":sum(1 for t in vetoed if _float(t.get("pnl"))>0),
        "delta":{"total_pnl":round(_float(shadow.get("total_pnl"))-_float(base.get("total_pnl")),4),
                 "win_rate_pct":round(_float(shadow.get("win_rate_pct"))-_float(base.get("win_rate_pct")),2),
                 "max_drawdown":round(_float(shadow.get("max_drawdown"))-_float(base.get("max_drawdown")),4),
                 "trades":int(shadow.get("trades",0))-int(base.get("trades",0))}}
