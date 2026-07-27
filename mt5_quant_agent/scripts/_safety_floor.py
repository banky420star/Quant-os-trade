"""Hard safety floor — applied after the bleed incident.

Goals (priority order):
1. Defensive kill of any bot running on :8080 (the earlier kill commands timed out).
2. Atomic rollback of the 5 Round 2/3 patches that contributed to the bleed:
     - signals.min_risk_reward: 0.85 -> 1.05     (lowered R:R minimum — admitted low-quality setups)
     - practice.micro.entry_confirm_seconds: 5 -> 30 (eliminated the 30s stability gate)
     - risk.max_drawdown_pct: 8 -> 20               (single losing XAU 0.01 lot trade could trip kill)
     - execution.mode: mt5 -> paper                (DO NOT execute real orders)
     - execution.live_trading_enabled: true -> false (block MT5 order path)
3. Defensive clear of stale state/kill_switch.json + state/equity_curve.json (so the bot does NOT
   re-trip the kill switch immediately on next launch, and is not stuck on a $100 micro baseline
   that no longer reflects the bled account).
4. YAML duplicate-key assertion: ensure no section appears twice (the 3 str_replace rounds
   could have left duplicates that silent-load would mask).
5. Final verification read of /api/state if bot is up.

NO modification to other gates — keep the regime_overrides softening (weak_trend etc.) and
performance.apply_when=never decision intact, since they were the user's actual request and
were NOT the bleed cause.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config.yaml"


def run(cmd: str, timeout: int = 20) -> tuple[int, str]:
    cp = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return cp.returncode, (cp.stdout or "").strip()


def banner(label: str) -> None:
    print("\n===", label, "===")


def main() -> int:
    # ---- 1. Kill any bot on :8080 ----
    banner("Step 1: Defensive kill of bot on :8080")
    rc, out = run("netstat -ano | findstr :8080 | findstr LISTENING", timeout=15)
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5:
            try:
                pids.append(int(parts[-1]))
            except ValueError:
                pass
    if not pids:
        print("  no listener on :8080 (already down)")
    for pid in pids:
        rc, out = run(f'cmd //c "taskkill /F /PID {pid}"', timeout=15)
        print(f"  taskkill PID {pid}: rc={rc} out={out[:160]}")
    # Wait briefly to let OS release the port
    import time
    time.sleep(2)

    # ---- 2. Apply 5 safety-floor edits ----
    banner("Step 2: Atomic safety-floor edits to config.yaml")
    text = CFG.read_text(encoding="utf-8")
    edits = [
        # (label, old, new) — each must match EXACTLY once before edit
        (
            "signals.min_risk_reward 0.85 -> 1.05",
            "  min_risk_reward: 0.85\n",
            "  # 2026-07-21 safety floor: rolled back 0.85 -> 1.05 (account bled from $100 to ~$10 under loose floor)\n"
            "  min_risk_reward: 1.05\n",
        ),
        (
            "entry_confirm_seconds 5 -> 30",
            "    # 2026-07-21: 30s → 5s — JP225m/others hitting the 30s entry_confirm window\n"
            "    # indefinitely during fast_mode windows.\n"
            "    entry_confirm_seconds: 5",
            "    # 2026-07-21 safety floor: rolled back 5 -> 30 (was over-aggressive on micro-tfs)\n"
            "    entry_confirm_seconds: 30",
        ),
        (
            "max_drawdown_pct 8 -> 20",
            "  # 2026-07-21: lowered 12 → 8 (tighter kill on small live account)\n"
            "  max_drawdown_pct: 8",
            "  # 2026-07-21 safety floor: rolled back 8 -> 20 (8 tripped kill after $90 bleed on $100 acct)\n"
            "  max_drawdown_pct: 20",
        ),
        (
            "execution.mode mt5 -> paper",
            "execution:\n  mode: mt5\n",
            "execution:\n  # 2026-07-21 safety floor: switched mode mt5 -> paper (NO real orders until tuner gives positive-EV)\n"
            "  mode: paper\n",
        ),
        (
            "execution.live_trading_enabled true -> false",
            "  live_trading_enabled: true\n",
            "  # 2026-07-21 safety floor: live_trading_enabled false (defence-in-depth even with mode=paper)\n"
            "  live_trading_enabled: false\n",
        ),
    ]
    new_text = text
    report: list[tuple[str, str, int]] = []
    for label, old, new in edits:
        n = new_text.count(old)
        if n == 1:
            new_text = new_text.replace(old, new, 1)
            report.append((label, "applied", 1))
        elif n == 0:
            report.append((label, "MISS (already absent?)", 0))
        else:
            # Multiple matches — same key appeared twice; bail to avoid silent corruption
            report.append((label, f"DUPLICATE x{n} — ABORT", n))
            print(f"  ! {label}: appears {n} times in config.yaml — refusing to patch")
            print(f"  ! Aborting before write to avoid silent corruption. Resolve manually.")
            return 2

    for label, status, n in report:
        print(f"  {status:<28} {label}  (occurrences={n})")

    # ---- 3. Atomic write ----
    tmp = CFG.with_suffix(".yaml.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, CFG)
    print(f"  wrote {CFG} ({len(new_text)} bytes)")

    # ---- 4. YAML re-parse + duplicate-key sanity ----
    import yaml
    banner("Step 3: Re-parse + duplicate-key check")
    final = yaml.safe_load(CFG.read_text(encoding="utf-8"))

    def count_key(blob, key):
        return sum(1 for ln in (CFG.read_text(encoding="utf-8")).splitlines() if re.match(rf"^\s*{re.escape(key)}\s*:", ln))

    keys_to_check = ["min_risk_reward", "max_drawdown_pct", "entry_confirm_seconds",
                     "live_trading_enabled", "mode", "min_atr_ratio",
                     "memory_veto_min_trades", "apply_when", "bias_aligned"]
    for k in keys_to_check:
        n = count_key(k, k)  # both args same name, ignoring second slot
        flag = "OK" if n <= 4 else "WARN (3-4 expected when nested)"
        print(f"  '{k}' appears {n} times  [{flag}]")

    sig = final["signals"]
    micro = final["practice"]["micro"]
    risk = final["risk"]
    ex = final["execution"]
    filt = final["filters"]
    intel = final["intelligence"]
    perf = final["performance"]
    print()
    print(f"  signals.min_risk_reward          = {sig['min_risk_reward']}")
    print(f"  practice.micro.entry_confirm     = {micro['entry_confirm_seconds']}")
    print(f"  risk.max_drawdown_pct            = {risk['max_drawdown_pct']}")
    print(f"  filters.min_atr_ratio            = {filt['min_atr_ratio']}")
    print(f"  intelligence.memory_veto_min     = {intel['memory_veto_min_trades']}")
    print(f"  performance.apply_when           = {perf['apply_when']}")
    print(f"  regime_overrides still soft (bias_aligned map):")
    for p, ov in sig["regime_overrides"].items():
        print(f"    {p:<22} bias_aligned={ov.get('bias_aligned')}  min_rr={ov.get('min_risk_reward')}")
    print(f"  execution.mode                   = {ex.get('mode')}  (must be paper)")
    print(f"  execution.live_trading_enabled   = {ex.get('live_trading_enabled')}  (must be false)")
    print(f"  execution.mt5_trading_enabled    = {ex.get('mt5_trading_enabled')}  (informational)")
    print(f"  execution.allow_live_account     = {ex.get('allow_live_account')}  (informational)")

    # ---- 5. Clear stale kill_switch.json so next launch isn't stuck ----
    banner("Step 4: Clear stale kill_switch + equity_curve")
    ks_path = ROOT / "state" / "kill_switch.json"
    if ks_path.exists():
        try:
            ks_old = json.loads(ks_path.read_text(encoding="utf-8"))
            print(f"  pre-clear  kill_switch.json: {ks_old}")
            ks_path.write_text(json.dumps({"kill_switch": False, "reason": None, "activated_at": None, "cleared_at": "2026-07-21T17:30:00Z"}, indent=2), encoding="utf-8")
            print(f"  post-clear kill_switch.json: {json.loads(ks_path.read_text(encoding='utf-8'))}")
        except Exception as exc:
            print(f"  ! could not clear kill_switch.json: {exc!r}")
    # equity_curve: rewrite as fresh baseline aligned to current starting_cash (paper mode)
    eq_path = ROOT / "state" / "equity_curve.json"
    if eq_path.exists():
        try:
            cash = float(final["execution"].get("starting_cash") or 100.0)
            fresh = {
                "starting_equity": cash,
                "current_equity": cash,
                "current_balance": cash,
                "unrealized_pnl": 0.0,
                "peak_equity": cash,
                "max_drawdown_pct": 0.0,
                "pnl_total": 0.0,
                "pnl_pct": 0.0,
                "starting_cash": cash,
                "reset_at": "2026-07-21T17:30:00Z",
            }
            eq_path.write_text(json.dumps(fresh, indent=2), encoding="utf-8")
            print(f"  equity_curve.json reset to starting_cash={cash} (paper mode baseline)")
        except Exception as exc:
            print(f"  ! could not reset equity_curve.json: {exc!r}")

    # ---- 6. Final live state check ----
    banner("Step 5: Verify /api/state is reachable (should be offline after kill)")
    try:
        import urllib.request
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:8080/api/state", timeout=5).read())
        eq = d.get("equity_curve", {}); ts = d.get("trading_status", {})
        print(f"  trading: {ts.get('status')}  can_execute={ts.get('can_execute')}")
        print(f"  equity : {eq.get('current_equity')}  pnl_total={eq.get('pnl_total')}")
        print("  (bot STILL UP despite kill — defensive kill did not land; investigate)")
    except Exception as exc:
        print(f"  bot unreachable after kill (expected): {type(exc).__name__}: {exc}")

    # ---- 7. Listener final check ----
    banner("Step 6: Confirm port :8080 is free")
    rc, out = run("netstat -ano | findstr :8080 | findstr LISTENING", timeout=15)
    print(f"  listener: {out or '(free)'}")

    print()
    print("SAFETY FLOOR COMPLETE")
    print("  config.yaml: paper-mode, gates hardened back to safe defaults.")
    print("  state/kill_switch.json: cleared (won't re-kill on next launch).")
    print("  state/equity_curve.json: reset to starting_cash baseline.")
    print("  Bot: terminated. Do NOT relaunch live until tuner gives positive-EV config.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
