"""Spawned rebuild + restart helper.
1. Rebuilds state/trade_log.json from scripts/rebuild_trade_log.py
2. Sets state/profile_switch.json to trigger bot restart on next cycle
3. Verifies /api/state
"""
import json, os, subprocess, sys, time
sys.stdout.reconfigure(encoding="utf-8")

ROOT = r"C:\Users\Administrator\Desktop\new task\mt5_quant_agent"
os.chdir(ROOT)

print("=== Step 1: rebuild trade_log.json ===")
r = subprocess.run([sys.executable, "scripts/rebuild_trade_log.py", "--days", "60"], capture_output=True, text=True, timeout=120)
print("rc:", r.returncode)
print("stdout:", r.stdout[-1200:])
if r.stderr:
    print("stderr:", r.stderr[-600:])

print()
print("=== Step 2: size after rebuild ===")
sz = os.path.getsize("state/trade_log.json")
print(f"trade_log.json size={sz} bytes")

print()
print("=== Step 3: peek at trade_log.json structure ===")
try:
    d = json.loads(open("state/trade_log.json", encoding="utf-8").read())
    if isinstance(d, list):
        print(f"LIST n={len(d)}")
        if d:
            print("first:", d[0])
    elif isinstance(d, dict):
        print("DICT keys:", list(d.keys())[:20])
        print(f"  total={d.get('total')} wins={d.get('wins')} losses={d.get('losses')}")
        print(f"  total_pnl={d.get('total_pnl')} win_rate_pct={d.get('win_rate_pct')}")
        print(f"  expectancy_R={d.get('expectancy_R')} avg_R={d.get('avg_R')}")
        trades = d.get("trades") or []
        print(f"  trades type={type(trades).__name__} len={len(trades) if isinstance(trades,list) else 'n/a'}")
        if isinstance(trades, list) and trades:
            print("  first trade:", trades[0])
except Exception as e:
    print("parse err:", e)

print()
print("=== Step 4: trigger restart via profile_switch.json ===")
try:
    ps = {"profile": "30-c2", "switch_at": time.time(), "reason": "rebuild+restart by user (no bash quote escape possible)"}
    with open("state/profile_switch.json", "w", encoding="utf-8") as f:
        json.dump(ps, f, indent=2)
    print("WROTE state/profile_switch.json", ps)
except Exception as e:
    print("profile_switch err:", e)

print()
print("=== Step 5: probe /api/state (give bot 5s to react) ===")
time.sleep(5)
try:
    import urllib.request
    d = json.loads(urllib.request.urlopen("http://127.0.0.1:8080/api/state", timeout=8).read())
    rt = d.get("runtime_mode", {})
    ts = d.get("trading_status", {})
    lp = d.get("live_portfolio", {})
    cs = d.get("candidate_signals", {})
    appr = d.get("approved_signals", {})
    cf = d.get("config", {})
    print(f"runtime.label={rt.get('label')}")
    print(f"runtime.detail[:200]={str(rt.get('detail',''))[:200]}")
    print(f"trading_status={ts.get('status')} can_execute={ts.get('can_execute')}")
    print(f"equity={lp.get('equity')} balance={lp.get('balance')} open={len(lp.get('open_positions') or [])}")
    print(f"candidates={cs.get('count')} approved={appr.get('count')}")
    print(f"cf.symbols count={len(cf.get('symbols') or [])} cf.execution.mode={(cf.get('execution') or {}).get('mode')}")
except Exception as e:
    print("/api/state probe err:", e)
