import json, sys, os
sys.stdout.reconfigure(encoding="utf-8")

for p in [
  "state/runtime_mode.json",
  "state/active_profile.json",
  "state/account.json",
  "state/approved_signals.json",
  "state/kill_switch.json",
  "state/risk_state.json",
  "state/health.json",
  "state/rejected_signals.json",
  "state/entry_staging.json",
  "state/candidate_signals.json",
  "state/adaptive_gates.json",
  "state/agent_lock.json",
  "state/daily_growth.json",
]:
    if not os.path.exists(p):
        print("---", p, ": MISSING")
        continue
    print("---", p, "---")
    try:
        d = json.loads(open(p, encoding="utf-8").read())
        s = json.dumps(d, indent=2, default=str)
        print(s[:2000])
        if len(s) > 2000:
            print(f"  ... ({len(s)} chars total, truncated)")
    except Exception as e:
        print("  err", e)
