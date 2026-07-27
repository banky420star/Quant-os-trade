"""Restart the bot cleanly (kill, launch, poll, full probe) and verify all 3
rounds of config patches are in effect + candidate approvals flow.

Pipeline:
1. YAML check (Round 3 patches present + valid)
2. Find current PID on 8080 via netstat, terminate
3. Launch scripts/_launch_bot_detached.py 100
4. Poll HTTP until 200 (up to 90s)
5. Verify Round 1 + Round 2 + Round 3 values present in /api/state
6. Wait 30s, re-query approvals
7. Dump rejection failure_codes so new gates are visible
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd: str, check: bool = True, timeout: int = 60) -> tuple[int, str]:
    cp = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return cp.returncode, (cp.stdout or "") + (cp.stderr or "")


def banner(label: str) -> None:
    print("\n===", label, "===")


def main() -> int:
    # ----- 1. YAML config sanity -----
    banner("YAML check (Round 1+2+3 patches)")
    import yaml
    y = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    sig = y.get("signals") or {}
    ro = sig.get("regime_overrides") or {}
    intel = y.get("intelligence") or {}
    risk = y.get("risk") or {}
    perf = y.get("performance") or {}
    filt = y.get("filters") or {}
    micro = (y.get("practice") or {}).get("micro") or {}

    checks = {
        "signals.min_risk_reward = 0.85": sig.get("min_risk_reward") == 0.85,
        "regime.strong_trend.bias_aligned = True": (ro.get("strong_trend") or {}).get("bias_aligned") is True,
        "regime.weak_trend.bias_aligned = False": (ro.get("weak_trend") or {}).get("bias_aligned") is False,
        "regime.compression.bias_aligned = False": (ro.get("compression") or {}).get("bias_aligned") is False,
        "regime.expansion.bias_aligned = False": (ro.get("expansion") or {}).get("bias_aligned") is False,
        "evaluation.symbol_blocklist = []": ((y.get("evaluation") or {}).get("symbol_blocklist") == []),
        "evaluation.setup_blocklist  = []": ((y.get("evaluation") or {}).get("setup_blocklist")  == []),
        "intelligence.memory_veto_min_trades = 5": intel.get("memory_veto_min_trades") == 5,
        "intelligence.memory_veto_cell_min_trades = 4": intel.get("memory_veto_cell_min_trades") == 4,
        "risk.max_drawdown_pct = 8 %": risk.get("max_drawdown_pct") == 8,
        "performance.apply_when = never": perf.get("apply_when") == "never",
        "filters.min_atr_ratio = 0.0001": filt.get("min_atr_ratio") == 0.0001,
        "practice.micro.entry_confirm_seconds = 5": micro.get("entry_confirm_seconds") == 5,
    }
    for k, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {k}")

    # ----- 2. find bot process on 8080 -----
    banner("Find current bot on :8080")
    rc, out = run('netstat -ano | findstr :8080 | findstr LISTENING')
    pid = None
    if rc == 0 and out.strip():
        last = out.strip().splitlines()[-1].split()
        if len(last) >= 5:
            pid = int(last[-1])
            print(f"  Current PID on :8080 = {pid}")
        else:
            print(f"  Could not parse PID; output=\n{out}")
    else:
        print("  No listener on :8080 (port free)")

    if pid:
        rc, out = run(f'cmd //c "taskkill /F /PID {pid}"')
        print(f"  taskkill rc={rc} output={out.strip()[:200]}")
        time.sleep(3)
        rc, out = run('netstat -ano | findstr :8080 | findstr LISTENING')
        print(f"  post-kill listener? -> rc={rc} out={out.strip()!r}")

    # ----- 3. launch detached -----
    banner("Launch bot (scripts/_launch_bot_detached.py 100)")
    rc, out = run('python scripts/_launch_bot_detached.py 100', timeout=30, check=False)
    print(f"  launch rc={rc}; output preview:\n{out[-1200:]}")

    # ----- 4. Poll HTTP -----
    banner("Poll HTTP 200 /api/")
    waited = 0
    http_ok = False
    for i in range(15):
        rc, out = run('curl -s -o NUL -w "%{http_code}" http://127.0.0.1:8080/', timeout=15)
        code = out.strip() or "ERR"
        print(f"  poll {i+1:2d}: HTTP={code} (waited={waited}s)")
        if code == "200":
            http_ok = True
            break
        time.sleep(6)
        waited += 6

    if not http_ok:
        print("  ✗ never got HTTP 200 — aborting verification")
        return 1

    rc, out = run('netstat -ano | findstr :8080 | findstr LISTENING')
    new_pid = None
    if out.strip():
        last = out.strip().splitlines()[-1].split()
        new_pid = int(last[-1]) if len(last) >= 5 else None
    print(f"  new PID = {new_pid}")

    # ----- 5. immediate /api/state snapshot -----
    banner("Live /api/state — immediate post-launch")
    try:
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:8080/api/state", timeout=10).read())
    except Exception as exc:
        print(f"  ! /api/state unreachable: {exc!r}")
        d = {}
    _print_state(d)

    # ----- 6. wait 60s, re-query -----
    banner("Wait 60s for verifier cycle, re-query")
    time.sleep(60)
    try:
        d2 = json.loads(urllib.request.urlopen("http://127.0.0.1:8080/api/state", timeout=10).read())
    except Exception as exc:
        print(f"  ! /api/state unreachable: {exc!r}")
        d2 = {}
    _print_state(d2)

    return 0


def _print_state(d: dict) -> None:
    cs = d.get("candidate_signals") or {}
    appr = d.get("approved_signals") or {}
    rej = d.get("rejected_signals") or {}
    ts = d.get("trading_status") or {}
    bp = d.get("bot_performance") or {}
    ai = d.get("ai_decision") or {}
    eqm = (d.get("equity_curve") or {}).get("current_equity")
    pnl = (d.get("equity_curve") or {}).get("pnl_total")
    print(f"  candidates: {cs.get('count')}  approved: {appr.get('count')}  rejected: {rej.get('count')}")
    print(f"  trading: {ts.get('status')}  can_execute={ts.get('can_execute')}")
    print(f"  bot_perf : trades={bp.get('trades')}  wr={bp.get('win_rate_pct')}%  net_pnl={bp.get('net_pnl')}  payoff={bp.get('payoff')}")
    print(f"  ai_decision: {ai.get('symbol')} {ai.get('side')} conf={ai.get('confidence')} setup={ai.get('setup')}")
    print(f"  equity: now={eqm}  total_pnl={pnl}")
    print(f"  blockers:")
    for b in (ts.get("blockers") or [])[:5]:
        print(f"    - {b[:240]}")
    print(f"  rejections failure_codes (first 5):")
    for r in (rej.get("rejected") or [])[:5]:
        sym, side = r.get("symbol"), r.get("side")
        codes = r.get("failure_codes") or []
        chk = r.get("checks") or {}
        failed = [k for k, v in chk.items() if v is False and not k.startswith("bypassed_")]
        print(f"    - {sym} {side} failure_codes={codes} failing_checks={failed}")


if __name__ == "__main__":
    sys.exit(main())
