import json, sys, os
sys.stdout.reconfigure(encoding='utf-8')

print("=== A) config.yaml live flags ===")
import yaml
y = yaml.safe_load(open("mt5_quant_agent/config.yaml", encoding="utf-8"))
ex = y["execution"]
print(f"mode={ex.get('mode')!r}  live_trading_enabled={ex.get('live_trading_enabled')!r}  mt5_trading_enabled={ex.get('mt5_trading_enabled')!r}  allow_live_account={ex.get('allow_live_account')!r}")
sig = y.get("signals", {})
print(f"signals.min_risk_reward={sig.get('min_risk_reward')}")
risk = y.get("risk", {})
print(f"risk.max_drawdown_pct={risk.get('max_drawdown_pct')}  max_daily_loss_pct={risk.get('max_daily_loss_pct')}  max_loss_per_trade_usd={risk.get('max_loss_per_trade_usd')}")
trade = y.get("trading", {})
print(f"trading.max_open_per_symbol={trade.get('max_open_per_symbol')}  allow_pyramiding={trade.get('allow_pyramiding')}  max_open_positions={trade.get('max_open_positions')}")
pmicro = y.get("practice", {}).get("micro", {})
print(f"practice.micro.symbols count={len(pmicro.get('symbols') or [])}")

print()
print("=== B) profiles/30-real.yaml live flags (overlay check) ===")
y30 = yaml.safe_load(open("mt5_quant_agent/profiles/30-real.yaml", encoding="utf-8"))
print(f"profile execution.mode={y30.get('execution',{}).get('mode')}  live={y30.get('execution',{}).get('live_trading_enabled')}")
print(f"profile signals.min_risk_reward={y30.get('signals',{}).get('min_risk_reward')}")
print(f"profile practice.micro.symbols count={len(y30.get('practice',{}).get('micro',{}).get('symbols') or [])}")

print()
print("=== C) state files (size + mtime) ===")
state_dir = "mt5_quant_agent/state"
for f in sorted(os.listdir(state_dir)):
    p = os.path.join(state_dir, f)
    if not os.path.isfile(p):
        continue
    sz = os.path.getsize(p)
    mt = os.path.getmtime(p)
    print(f"{f:<40} {sz:>10} bytes  mtime_epoch={mt:.0f}")

print()
print("=== D) trade_log.json + meta + paper_trades.json ===")
for p in [
    "mt5_quant_agent/state/trade_log.json",
    "mt5_quant_agent/state/trade_log_meta.json",
    "mt5_quant_agent/state/paper_trades.json",
    "mt5_quant_agent/state/paper_orders.json",
    "mt5_quant_agent/state/paper_positions.json",
    "mt5_quant_agent/state/live_orders.json",
    "mt5_quant_agent/state/live_positions.json",
]:
    if not os.path.exists(p):
        print(f"{p}: MISSING")
        continue
    try:
        d = json.loads(open(p, encoding="utf-8").read())
        if isinstance(d, list):
            print(f"{p}: list n={len(d)}")
            for r in d[-3:]:
                ts = r.get("close_time") or r.get("open_time") or r.get("timestamp") if isinstance(r, dict) else None
                pnl = r.get("net_pnl") or r.get("pnl") if isinstance(r, dict) else None
                sym = r.get("symbol") if isinstance(r, dict) else None
                tag = r.get("exit_reason") or r.get("reason") if isinstance(r, dict) else None
                print(f"   last: sym={sym} pnl={pnl} ts={ts} tag={tag}")
        elif isinstance(d, dict):
            keys = list(d.keys())[:15]
            print(f"{p}: dict keys={keys}")
            for k, v in d.items():
                if isinstance(v, list):
                    print(f"   {k}: list n={len(v)}")
                    if v and isinstance(v[-1], dict):
                        print(f"     last: {v[-1]}")
                elif isinstance(v, dict):
                    print(f"   {k}: dict keys={list(v.keys())[:6]}")
                else:
                    print(f"   {k}: {v}")
    except Exception as e:
        print(f"{p}: parse error {e}")

print()
print("=== E) /api/state dashboard snapshot ===")
try:
    import urllib.request
    d = json.loads(urllib.request.urlopen("http://127.0.0.1:8080/api/state", timeout=6).read())
    rt = d.get("runtime_mode", {})
    ts = d.get("trading_status", {})
    lp = d.get("live_portfolio", {})
    cs = d.get("candidate_signals", {})
    appr = d.get("approved_signals", {})
    cf = d.get("config", {})
    print(f"runtime.label={rt.get('label')}")
    det = rt.get("detail", "")
    if isinstance(det, list):
        det = det[0] if det else ""
    print(f"runtime.detail (first 250 chars): {str(det)[:250]}")
    print(f"trading_status.status={ts.get('status')} can_execute={ts.get('can_execute')} reason={ts.get('reason') or ts.get('block_reason')}")
    print(f"equity={lp.get('equity')} balance={lp.get('balance')} open_positions={len(lp.get('open_positions') or [])}")
    print(f"candidates={cs.get('count')}  approved={appr.get('count')}")
    print(f"cf.symbols count={len(cf.get('symbols') or [])}  cf.execution.mode={(cf.get('execution') or {}).get('mode')}")
except Exception as e:
    print(f"dashboard probe error: {e}")

print()
print("=== F) bot listener on 8080 ===")
import subprocess
try:
    out = subprocess.check_output(["netstat", "-ano"], text=True)
    for line in out.splitlines():
        if ":8080" in line and "LISTENING" in line:
            print("LISTENING:", line.strip())
except Exception as e:
    print("netstat err:", e)
