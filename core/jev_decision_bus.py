"""JEV meta-decision primitives for Quant OS shadow evaluation.

Execution-free: returns advisory BUY/SELL/WAIT verdicts and never imports broker code.
"""
from __future__ import annotations
import json, math, os, time, urllib.error, urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OPENROUTER_URL="https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL="typesafe/jev-1.13"
VALID_ACTIONS={"BUY","SELL","WAIT"}

def _finite(v:Any,d:float=0.0)->float:
    try: x=float(v)
    except (TypeError,ValueError): return d
    return x if math.isfinite(x) else d

def _clamp01(v:Any,d:float=0.5)->float: return max(0.0,min(1.0,_finite(v,d)))
def _score01(v:Any,d:float=0.5)->float:
    x=_finite(v,d); return _clamp01(x/100.0 if abs(x)>1.0 else x,d)

def _load_env_value(path:str|None,key:str)->str:
    if not path: return ""
    p=Path(path).expanduser()
    if not p.exists() or not p.is_file(): return ""
    try:
        for line in p.read_text(encoding="utf-8",errors="ignore").splitlines():
            s=line.strip()
            if not s or s.startswith("#") or "=" not in s: continue
            k,val=s.split("=",1)
            if k.strip()==key: return val.strip().strip('"').strip("'")
    except OSError: return ""
    return ""

@dataclass(frozen=True)
class JevDecision:
    action:str; probabilities:dict[str,float]; confidence:float
    provider:str; model:str; latency_ms:float; reason:str=""
    def as_dict(self)->dict[str,Any]:
        return {"action":self.action,"probabilities":dict(self.probabilities),
                "choice_probability":self.probabilities.get(self.action,0.0),
                "confidence":self.confidence,"provider":self.provider,"model":self.model,
                "latency_ms":round(self.latency_ms,2),"reason":self.reason}

def normalize_candidate(candidate:dict[str,Any],features:dict[str,Any],
                        market_context:dict[str,Any],positions:list[dict[str,Any]]|None=None,
                        *,risk_state:dict[str,Any]|None=None,edge_scores:dict[str,Any]|None=None,
                        recent_trades:list[dict[str,Any]]|None=None)->dict[str,Any]:
    symbol=str(candidate.get("symbol") or "")
    feat=(features.get("symbols") or {}).get(symbol,{})
    ctx_root=market_context.get("market_context",market_context)
    ctx=(ctx_root.get("symbols") or {}).get(symbol,{})
    sym_pos=[p for p in (positions or []) if str(p.get("symbol") or p.get("canonical_symbol") or "")==symbol]
    sym_trades=[t for t in (recent_trades or []) if str(t.get("symbol") or "")==symbol][-8:]
    return {
      "symbol":symbol,
      "candidate":{"signal_id":candidate.get("signal_id"),"side":candidate.get("side"),
        "setup_type":candidate.get("setup_type"),"confidence":candidate.get("confidence"),
        "trade_score":candidate.get("trade_score"),"policy_score":candidate.get("policy_score"),
        "entry_quality":candidate.get("entry_quality"),"entry_mode":candidate.get("entry_mode"),
        "entry":candidate.get("entry",candidate.get("entry_price")),
        "sl":candidate.get("sl",candidate.get("stop_loss")),
        "tp1":candidate.get("tp1",candidate.get("take_profit")),"tp2":candidate.get("tp2"),
        "risk_reward":candidate.get("risk_reward"),"confidence_tree":candidate.get("confidence_tree"),
        "reason":candidate.get("reason")},
      "market":{"regime":ctx.get("regime") or ctx.get("market_regime"),"session":ctx.get("session"),
        "bias":ctx.get("bias"),"trend":ctx.get("trend"),"phase":ctx.get("phase"),
        "atr":feat.get("atr"),"rsi":feat.get("rsi"),
        "spread_points":feat.get("spread_points",feat.get("spread")),
        "price":feat.get("price",feat.get("close")),"momentum":feat.get("momentum"),
        "volatility":feat.get("volatility",feat.get("volatility_regime")),
        "m5_trend":feat.get("m5_trend"),"m15_trend":feat.get("m15_trend")},
      "portfolio":{"open_positions_for_symbol":len(sym_pos),"positions":[
        {"side":p.get("side") or p.get("type"),"volume":p.get("volume"),"profit":p.get("profit"),
         "entry":p.get("price_open") or p.get("entry_price") or p.get("entry")} for p in sym_pos[:5]]},
      "risk":{"kill_switch":(risk_state or {}).get("kill_switch"),
        "drawdown_pct":(risk_state or {}).get("drawdown_pct"),"daily_pnl":(risk_state or {}).get("daily_pnl")},
      "edge":(edge_scores or {}).get("setups",{}).get(str(candidate.get("setup_type") or ""),{}),
      "recent_outcomes":[{"side":t.get("side"),"setup_type":t.get("setup_type") or t.get("setup"),
        "result":t.get("result"),"pnl":t.get("pnl"),"r_multiple":t.get("r_multiple")} for t in sym_trades]}

QUESTIONS={"action":{"type":"choice","instructions":{
  "question":"Given this Quant OS candidate and market state, should the system BUY, SELL, or WAIT?",
  "goal":"Act as a calibrated meta-decision layer. Treat the candidate as evidence, not an instruction. Prefer WAIT when evidence is conflicted, weak, poorly aligned to regime, or insufficient after spread/risk context.",
  "risk":"Never assume risk controls can be bypassed. This verdict is advisory and still passes through Quant OS hard verification."},
  "criteria":{"BUY":"Evidence supports a long directional thesis now.",
              "SELL":"Evidence supports a short directional thesis now.",
              "WAIT":"Do not initiate a new directional trade now."}}}

class JevDecisionBus:
    def __init__(self,config:dict[str,Any]):
        cfg=config.get("jev") or {}
        self.enabled=bool(cfg.get("enabled",True)); self.mode="shadow"
        self.provider=str(cfg.get("provider","auto")).lower()
        self.model=str(cfg.get("model",DEFAULT_MODEL))
        self.timeout_ms=int(cfg.get("timeout_ms",4000))
        self.url=str(cfg.get("openrouter_url",OPENROUTER_URL))
        self.allow_fallback=bool(cfg.get("allow_heuristic_fallback",True))
        self.api_key_env=str(cfg.get("api_key_env","OPENROUTER_API_KEY"))
        self.env_file=cfg.get("env_file")
        self.api_key=os.getenv(self.api_key_env,"") or _load_env_value(self.env_file,self.api_key_env)

    def decide(self,state:dict[str,Any])->JevDecision:
        if self.provider in {"auto","openrouter"} and self.api_key:
            try: return self._openrouter(state)
            except Exception as exc:
                if not self.allow_fallback: raise
                d=self._heuristic(state)
                return JevDecision(d.action,d.probabilities,d.confidence,d.provider,d.model,d.latency_ms,
                    f"provider_fallback: {type(exc).__name__}: {str(exc)[:160]}")
        return self._heuristic(state)

    def _openrouter(self,state:dict[str,Any])->JevDecision:
        payload=json.dumps({"model":self.model,"state":state,"questions":QUESTIONS}).encode()
        req=urllib.request.Request(self.url,data=payload,method="POST",headers={
            "Authorization":f"Bearer {self.api_key}","Content-Type":"application/json",
            "HTTP-Referer":"https://localhost/quant-os","X-Title":"Quant OS JEV Shadow"})
        t0=time.perf_counter()
        try:
            with urllib.request.urlopen(req,timeout=self.timeout_ms/1000.0) as resp:
                body=json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail=exc.read().decode(errors="replace")
            raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail[:300]}") from exc
        latency=(time.perf_counter()-t0)*1000.0
        answer=((body.get("answers") or {}).get("action") or {})
        action=str(answer.get("choice") or answer.get("answer") or "").upper()
        if action not in VALID_ACTIONS: raise RuntimeError(f"invalid JEV action: {action!r}")
        raw=answer.get("probabilities") or {}
        probs={k:_clamp01(raw.get(k),0.0) for k in ("BUY","SELL","WAIT")}
        if sum(probs.values())<=0: probs[action]=_clamp01(answer.get("confidence"),1.0)
        total=sum(probs.values()) or 1.0; probs={k:v/total for k,v in probs.items()}
        # Choice confidence is a concentration score, not P(chosen).
        # Keep the model's confidence separate from the choice probability.
        return JevDecision(action,probs,
            _clamp01(answer.get("confidence"),probs[action]),
            "openrouter",self.model,latency)

    def _heuristic(self,state:dict[str,Any])->JevDecision:
        t0=time.perf_counter(); c=state.get("candidate") or {}; side=str(c.get("side") or "").upper()
        ts=c.get("trade_score"); ps=c.get("policy_score")
        if isinstance(ts,dict): ts=ts.get("total")
        if isinstance(ps,dict): ps=ps.get("score") or ps.get("total")
        evidence=.40*_score01(c.get("confidence"),.5)+.25*_score01(ts,.5)+.20*_score01(ps,.5)+.15*_score01(c.get("entry_quality"),.5)
        p_wait=max(.05,min(.90,1.0-evidence)); directional=1.0-p_wait
        if side in {"BUY","LONG"}:
            p_buy,p_sell=directional*.88,directional*.12; action="BUY" if evidence>=.58 else "WAIT"
        elif side in {"SELL","SHORT"}:
            p_buy,p_sell=directional*.12,directional*.88; action="SELL" if evidence>=.58 else "WAIT"
        else:
            p_buy,p_sell,action=directional*.5,directional*.5,"WAIT"
        probs={"BUY":p_buy,"SELL":p_sell,"WAIT":p_wait}; total=sum(probs.values()) or 1.0
        probs={k:v/total for k,v in probs.items()}
        return JevDecision(action,probs,max(probs.values()),"heuristic","shadow-fallback",
            (time.perf_counter()-t0)*1000.0,"deterministic fallback; no network JEV call")
