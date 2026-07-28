"""Last-resort grep: find the literal s('Max daily loss') string and
the constant it prints (e.g. 0.08 / 8 / max_daily_loss_pct) inside
risk_manager.py and risk_loop.py. Then walk back to find the
config key the comparison reads.

Run inside the project root. Pure stdlib, no deps.

Print the exact line, function name, config key, and comparison
operator so we can patch the right one surgically.
"""
from __future__ import annotations
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

target_files = [
    os.path.join(ROOT, "mt5_quant_agent", "core", "risk_manager.py"),
    os.path.join(ROOT, "mt5_quant_agent", "loops", "risk_loop.py"),
    os.path.join(ROOT, "mt5_quant_agent", "core", "daily_growth.py"),
]

# Patterns that helped before:
#   * "Max daily loss" string
#   * "0.08" or ": 8" or "8%"
#   * the config-key token that might be missing
needles = [
    "Max daily loss",
    "max_daily_loss_pct",
    "max_daily_loss",
    "daily_loss",
    "session_daily",
    "0.08",
    "8.0",
    ": 8",
    "max_loss_pct",
    "max_loss",
]

scopes = []
for path in target_files:
    if not os.path.exists(path):
        print(f"[missing] {path}")
        continue
    src_lines = open(path, encoding="utf-8").read().splitlines()
    base = os.path.basename(path)
    print(f"\n##### {path}  (lines={len(src_lines)}) #####")
    for needle in needles:
        hits = [(i, l) for i, l in enumerate(src_lines, 1) if needle in l]
        for i, l in hits[:5]:
            print(f"  {i:>4}: {l.strip()[:200]}")

# Also show the practice.* and risk.* front of growth.yaml after the last patch
import yaml

PROFILE = os.path.join(ROOT, "mt5_quant_agent", "profiles", "growth.yaml")
try:
    d = yaml.safe_load(open(PROFILE, encoding="utf-8")) or {}
    print("\n##### profiles/growth.yaml relevant sections #####")
    for top in ("practice", "risk", "adaptation"):
        if top in d:
            sub = d[top]
            print(f"  {top}:")
            for k, v in (sub.items() if isinstance(sub, dict) else []):
                print(f"    {k}: {v}")
except Exception as e:
    print(f"profile read failed: {e}")
