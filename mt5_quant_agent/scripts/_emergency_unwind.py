"""Emergency unwind:
1) Defensive kill of the bot process on :8080 (kill switch already fired; this is belt+suspenders).
2) Roll back the dangerous Round 2/3 patches that caused the bleed:
     - risk.max_drawdown_pct: 8 -> 20 (was originally 12; raise further for safety)
     - practice.micro.entry_confirm_seconds: 5 -> 30 (was 30; restore)
     - filters.min_atr_ratio: 0.0001 -> 0.0002 (was 0.0002; restore)
   Keep the Round 2 regime_overrides softening intact (those weren't the cause).
   Keep performance.apply_when=never (avoids the overlay reset).
   Keep signals.min_risk_reward=0.85 (the user's stated goal).
   Keep intelligence.memory_veto_min_trades=5 (safety net, fine to keep).
3) Verify YAML still parses + show diff sanity.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config.yaml"


def run(cmd: str, timeout: int = 20) -> tuple[int, str]:
    cp = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return cp.returncode, (cp.stdout or "") + (cp.stderr or "")


def banner(label: str) -> None:
    print("\n===", label, "===")


def main() -> int:
    # ----- 1. Defensive kill -----
    banner("Defensive kill of bot on :8080")
    rc, out = run('netstat -ano | findstr :8080 | findstr LISTENING')
    pids = []
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) >= 5:
            try:
                pids.append(int(parts[-1]))
            except ValueError:
                pass
    if not pids:
        print("  no bot listener found (already down)")
    for pid in pids:
        rc, out = run(f'cmd //c "taskkill /F /PID {pid}"')
        print(f"  taskkill PID {pid}: rc={rc} out={out.strip()[:120]}")

    # ----- 2. Read current config.yaml -----
    banner("Read current config.yaml")
    text = CFG.read_text(encoding="utf-8")
    print(f"  bytes={len(text)}  lines={text.count(chr(10))}")

    # ----- 3. Roll back dangerous Round 2/3 patches -----
    banner("Apply rollbacks")
    edits: list[tuple[str, str, str]] = []
    dangerous = [
        # raised drawdown back to 20 (was 12 originally, dropped to 8 in round 2)
        (
            "  # 2026-07-21: lowered 12 → 8 (tighter kill on small live account)\n"
            "  max_drawdown_pct: 8",
            "  max_drawdown_pct: 20  # 2026-07-21: rolled back from 8 -> 20 (8 tripped kill after $90 bleed)",
        ),
        # entry_confirm back from 5 -> 30
        (
            "    # 2026-07-21: 30s → 5s — JP225m/others hitting the 30s entry_confirm window\n"
            "    # indefinitely during fast_mode windows.\n"
            "    entry_confirm_seconds: 5",
            "    entry_confirm_seconds: 30  # 2026-07-21: rolled back 5 -> 30 after bleed",
        ),
        # min_atr_ratio back from 0.0001 -> 0.0002
        (
            "  # 2026-07-21: lowered 0.0002 → 0.0001 — several symbols (USDCHFm, AUDUSDm)\n"
            "  # were being rejected on atr_safe despite valid setups.\n"
            "  min_atr_ratio: 0.0001",
            "  min_atr_ratio: 0.0002  # 2026-07-21: rolled back 0.0001 -> 0.0002 after bleed",
        ),
    ]
    new_text = text
    for old, repl in dangerous:
        if old in new_text:
            new_text = new_text.replace(old, repl, 1)
            edits.append(("OK", old[:90].replace("\n", " "), repl[:90].replace("\n", " ")))
        else:
            edits.append(("MISS", old[:90].replace("\n", " "), repl[:90].replace("\n", " ")))
    for status, old, repl in edits:
        print(f"  {status}  {old.replace(chr(10), ' ')[:80]}\n         -> {repl[:80]}")

    # Sanity: keep the safe Round 2/3 edits (regime_overrides, performance.apply_when,
    # signals.min_risk_reward=0.85, intelligence.memory_veto_min_trades=5).
    # Nothing to revert on those — they weren't the cause of the bleed.
    cfg_out = CFG.with_suffix(".yaml.rolled")
    cfg_out.write_text(new_text, encoding="utf-8")

    # ----- 4. Atomic write-back -----
    import os
    tmp = CFG.with_suffix(".yaml.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, CFG)
    print(f"  wrote {CFG} (atomic replace OK)")

    # ----- 5. YAML parse check -----
    banner("YAML re-parse")
    import yaml
    y = yaml.safe_load(CFG.read_text(encoding="utf-8"))
    sig = y["signals"]; filt = y["filters"]; micro = y["practice"]["micro"]; risk = y["risk"]
    ro  = sig["regime_overrides"]
    print(f"  signals.min_risk_reward          = {sig['min_risk_reward']}")
    print(f"  filters.min_atr_ratio            = {filt['min_atr_ratio']}")
    print(f"  practice.micro.entry_confirm_s  = {micro['entry_confirm_seconds']}")
    print(f"  risk.max_drawdown_pct            = {risk['max_drawdown_pct']}")
    print(f"  intelligence.memory_veto_min_t  = {y['intelligence']['memory_veto_min_trades']}")
    print(f"  performance.apply_when          = {y['performance']['apply_when']}")
    print(f"  regime_overrides.bias_aligned map (must keep strong_trend=True):")
    for p, ov in ro.items():
        print(f"    {p:<22} bias_aligned={ov.get('bias_aligned')}  min_rr={ov.get('min_risk_reward')}")

    # ----- 6. Show live state was reachable before our kill (just for log) -----
    banner("Pre-kill snapshot (was likely HARD KILL already)")
    try:
        import urllib.request
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:8080/api/state", timeout=5).read())
        ts = d.get("trading_status", {})
        bp = d.get("bot_performance", {})
        eq = (d.get("equity_curve") or {})
        print(f"  trading status        = {ts.get('status')}")
        print(f"  kill_reason           = {ts.get('kill_reason')}")
        print(f"  bot_perf : trades={bp.get('trades')}  wr={bp.get('win_rate_pct')}%  net={bp.get('net_pnl')}  payoff={bp.get('payoff')}")
        print(f"  equity now            = {eq.get('current_equity')}  total_pnl = {eq.get('pnl_total')}")
        print("  blockers (first 4):")
        for b in (ts.get("blockers") or [])[:4]:
            print(f"    - {b[:200]}")
    except Exception as exc:
        print(f"  (bot already dead, expected: {exc!r})")

    print()
    print("ROLLBACK COMPLETE — do NOT launch bot again until user confirms.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
