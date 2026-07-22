"""Probe tile HTML structure and JS render template from dashboard/index.html.

Outputs:
  1. Lines 1150-1230 (around livingParamsCard HTML tile)
  2. The complete renderLivingParamsTile function (so I can mimic style)
  3. The complete fetchLivingParams function (so I can mimic polling)
"""
import re
from pathlib import Path
src = Path("mt5_quant_agent/dashboard/index.html").read_text(encoding="utf-8")
lines = src.splitlines()

print(f"TOTAL LINES: {len(lines)}")
print()
print("--- livingParamsCard HTML region (lines 1140..1230) ---")
for i, l in enumerate(lines[1139:1230], start=1140):
    print(f"[{i}]: {l}")

print()
print("--- renderLivingParamsTile JS function ---")
# Find function start
start_idx = None
for i, l in enumerate(lines):
    if "renderLivingParamsTile" in l and ("function" in l or "async" in l):
        start_idx = i; break
if start_idx is not None:
    # Walk forward until next 'function' or top-level declaration
    end_idx = start_idx + 1
    while end_idx < len(lines):
        l2 = lines[end_idx]
        if re.match(r"^(function|async function|let _lpTimer|function fetchLivingParams\b)", l2):
            break
        end_idx += 1
    for j in range(start_idx, end_idx + 1):
        print(f"[{j+1}]: {lines[j]}")

print()
print("--- fetchLivingParams function ---")
m = re.search(r"async function fetchLivingParams[^}]*?^}", src, re.M | re.S)
if m: print(m.group(0)[:1500])
