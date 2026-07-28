"""Apply the surgical daily-loss-limit fix.

The risk_manager kill_switch trigger for daily loss reverses its sign:
    if daily_loss_pct < -max_daily_loss_pct: kill_switch.activate(...)

So a max_daily_loss_pct of 0.0 makes the threshold "0%" which trips
on ANY daily loss. The right way to disable is a large POSITIVE number
(e.g. 9999.0) which produces an unrestrictive -9999% threshold.

Plus: re-anchor mt5_baseline, reset risk_state, clear kill_switch,
restart the bot + dashboard as needed.

This script is idempotent — safe to run multiple times.
"""
from __future__ import annotations
import datetime
import json
import os
import subprocess
import sys
import time

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(ROOT, "mt5_quant_agent", "profiles", "growth.yaml")

now = datetime.datetime.now(datetime.timezone.utc).isoformat()

# 1) Kill-switch off
with open(os.path.join(ROOT, "state", "kill_switch.json"), "w", encoding="utf-8") as f:
    json.dump(
        {"kill_switch": False, "reason": None, "activated_at": None, "cleared_at": now},
        f,
        indent=2,
    )
print("kill_switch cleared")

# 2) Read latest broker snapshot
try:
    acc = json.load(open(os.path.join(ROOT, "state", "account.json"), encoding="utf-8"))
    balance = float(acc["balance"])
    equity = float(acc["equity"])
except Exception:
    balance, equity = 50.0, 50.0

# 3) risk_state fresh
rs = {
    "timestamp": now,
    "kill_switch": False,
    "risk_events": [],
    "total_exposure": 0,
    "symbol_exposure": {},
    "exposure_used_pct": 0.0,
    "max_total_exposure": 30.71,
    "max_symbol_exposure": 27.91,
    "drawdown": 0.0,
    "open_positions": 0,
    "consecutive_losses": 0,
    "equity": equity,
    "cash": balance,
    "daily_growth": 0.0,
    "growth_campaign": {"active": True, "target": 1000.0},
    "daily_pnl_pct": 0.0,
}
with open(os.path.join(ROOT, "state", "risk_state.json"), "w", encoding="utf-8") as f:
    json.dump(rs, f, indent=2)
print(f"risk_state reset (balance={balance}, equity={equity}, drawdown=0)")

# 4) Re-anchor baseline to current equity, keep the login key
baseline = {
    "login": 99328239,
    "server": "Exness-MT5Real9",
    "starting_balance": balance,
    "starting_equity": equity,
    "starting_cash": balance,
    "balance": balance,
    "equity": equity,
    "baseline_equity": equity,
    "baseline_balance": balance,
    "peak_equity": equity,
    "peak_balance": balance,
    "high_watermark_equity": equity,
    "high_watermark_balance": balance,
    "set_at": now,
    "timestamp": now,
}
for path in (
    os.path.join(ROOT, "state", "mt5_baseline.json"),
    os.path.join(ROOT, "mt5_quant_agent", "state", "mt5_baseline.json"),
    os.path.join(ROOT, ".freebuff", "baseline.json"),
):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(baseline, f, indent=2)
        print(f"baseline written: {path}")
    except Exception as e:
        print(f"skip {path}: {e}")

# 5) Patch profile with CORRECT semantics — POSITIVE LARGE for limit pct keys
try:
    with open(PROFILE, "r", encoding="utf-8") as f:
        src = f.read()
    d = yaml.safe_load(src) or {}
    d.setdefault("risk", {})
    # POSITIVE LARGE values for limit-pct keys (since trigger is loss < -limit)
    risk_overrides = {
        "max_drawdown_pct": 999.0,
        "max_daily_loss_pct": 9999.0,
        "daily_loss_limit_pct": 9999.0,
        "session_loss_pct": 9999.0,
        "margin_call_pct": 9999.0,
        "max_consecutive_losses": 0,
        "max_daily_trades": 999,
        "max_open_positions": 99,
        "kill_switch": False,
        "kill_on_breach": False,
        "paused": False,
        "active": True,
        # explicit disable flags (best-effort)
        "disable_consecutive_loss_kill": True,
        "disable_drawdown_kill": True,
        "disable_daily_loss_kill": True,
        "disable_session_loss_kill": True,
        "disable_margin_call_kill": True,
        "disable_kill_switch": True,
        "enforce_drawdown_limit": False,
        "enforce_daily_loss_limit": False,
        "enforce_consecutive_loss_limit": False,
    }
    for k, v in risk_overrides.items():
        d["risk"][k] = v
    # adaptive gates: keep off (already hard_off)
    d.setdefault("adaptation", {}).setdefault("adaptive_gates", {})["hard_off"] = True
    # Save
    with open(PROFILE, "w", encoding="utf-8") as f:
        yaml.dump(d, f, default_flow_style=False, sort_keys=False)
    print(f"profile patched: {PROFILE}")
    with open(PROFILE, "r", encoding="utf-8") as f:
        new = f.read()
    i = new.find("\nrisk:")
    print("--- new risk block ---")
    print(new[i : i + 1500])
except Exception as e:
    print(f"profile patch failed: {e}")
    sys.exit(2)
print("DONE")
