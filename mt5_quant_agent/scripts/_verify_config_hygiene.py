"""One-shot config + bot hygiene probe (avoids bash-quoting issues).

Verifies:
1. YAML files parse cleanly.
2. Where memory_veto_min_trades is actually read (verifier vs intelligence).
3. Where apply_when resolves to in core/utils.py:load_config.
4. Whether shadow mode makes setup_blocklist / symbol_blocklist a no-op.
5. Live bot state + last 30s of approval flow.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def banner(label: str) -> None:
    print()
    print("===", label, "===")


def main() -> int:
    # ----- 1. YAML parse -----
    banner("YAML parse")
    try:
        import yaml
        for path in ("config.yaml", "profiles/30.yaml",
                     "profiles/30-real.yaml", "profiles/growth.yaml"):
            full = ROOT / path
            if not full.exists():
                print(f"  {path}: MISSING")
                continue
            y = yaml.safe_load(full.read_text(encoding="utf-8"))
            sig = (y.get("practice", {}) or {}).get("micro", {}) or {}
            print(f"  {path}: OK "
                  f"(practice.micro.min_risk_reward={sig.get('min_risk_reward')}, "
                  f"signals.min_risk_reward={(y.get('signals') or {}).get('min_risk_reward')}, "
                  f"performance.apply_when={(y.get('performance') or {}).get('apply_when')})")
    except Exception as exc:
        print(f"  YAML parse ERROR: {exc!r}")
        return 2

    # ----- 2. memory_veto_min_trades path -----
    banner("memory_veto_min_trades consumer")
    files_to_grep = list((ROOT / "core").glob("*.py")) + list((ROOT / "loops").glob("*.py"))
    matches: list[tuple[str, str]] = []
    for f in files_to_grep:
        try:
            src = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for ln, line in enumerate(src.splitlines(), start=1):
            if "memory_veto_min_trades" in line:
                matches.append((str(f.relative_to(ROOT)), f"{ln}: {line.strip()}"))
    if not matches:
        print("  (no consumer found — flag is inert)")
    else:
        for fp, line in matches[:25]:
            print(f"  {fp} | {line[:160]}")

    # ----- 3. apply_when overlay -----
    banner("apply_when / performance overlay")
    utils_src = (ROOT / "core" / "utils.py").read_text(encoding="utf-8")
    for ln, line in enumerate(utils_src.splitlines(), start=1):
        if "apply_when" in line or "performance" in line and "deep_merge" in line:
            print(f"  utils.py L{ln}: {line.strip()[:200]}")
    # Print the load_config body (signature is "def load_config(...)")
    m = re.search(r"def load_config\b.*?(?=^def |\Z)", utils_src, re.DOTALL | re.MULTILINE)
    if m:
        body = m.group(0)
        print()
        print(f"  load_config total length: {len(body)} chars (first 2000:)".strip())
        print(body[:2000])
    else:
        print("  load_config NOT FOUND in core/utils.py")

    # ----- 4. shadow-mode blocklist behaviour -----
    banner("evaluation_policy.shadow + blocklist logic")
    ep = (ROOT / "core" / "evaluation_policy.py").read_text(encoding="utf-8")
    for ln, line in enumerate(ep.splitlines(), start=1):
        if any(k in line for k in ("shadow", "setup_blocklist", "symbol_blocklist",
                                    "skip_below_score", "min_policy_score",
                                    "_should_skip", "block_reason", "evaluate_batch")):
            print(f"  L{ln}: {line.strip()[:200]}")

    # ----- 5. Live state snapshot -----
    banner("Live /api/state (current bot)")
    try:
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:8080/api/state", timeout=10).read())
    except Exception as exc:
        print(f"  Could not reach bot: {exc!r}")
        return 0

    appr = d.get("approved_signals") or {}
    rej = d.get("rejected_signals") or {}
    cs = d.get("candidate_signals") or {}
    ts = d.get("trading_status") or {}
    bp = d.get("bot_performance") or {}
    ai = d.get("ai_decision") or {}
    print(f"  candidates: {cs.get('count')}  approved: {appr.get('count')}  rejected: {rej.get('count')}")
    print(f"  trading: {ts.get('status')}  can_execute={ts.get('can_execute')}")
    print(f"  bot_perf : trades={bp.get('trades')}  wr={bp.get('win_rate_pct')}%  net_pnl={bp.get('net_pnl')}  payoff={bp.get('payoff')}")
    print(f"  ai_decision: {ai.get('symbol')} {ai.get('side')} conf={ai.get('confidence')} setup={ai.get('setup')}")
    print("  blockers:")
    for b in (ts.get("blockers") or [])[:5]:
        print(f"    - {b[:240]}")
    print("  last rejection(s) failure_codes (first 5 failures from last cycle):")
    for r in (rej.get("rejected") or [])[:3]:
        sym = r.get("symbol")
        codes = r.get("failure_codes") or []
        chk = r.get("checks") or {}
        failed = [k for k, v in chk.items() if v is False and not k.startswith("bypassed_")]
        print(f"    - {sym} failure_codes={codes} failing_checks={failed}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
