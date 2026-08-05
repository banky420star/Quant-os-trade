"""Robust source-level patch for core/daily_growth.py.

Steps:
1. Strip any prior bad insertions (broken guard, old helper).
2. Find `def evaluate_daily_growth(`.
3. Walk forward to find the closing triple-quote of the function docstring.
4. Insert the guard at line index docstring_close+1, with body indent 4 spaces.
5. Append the helper at the bottom (after verifying it's not already present).
6. py_compile must pass. If not, dump the surrounding lines and abort.
"""
from __future__ import annotations
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "mt5_quant_agent", "core", "daily_growth.py")
src = open(TARGET, encoding="utf-8").read()

# --- 1. STRIP BAD PRIOR ATTEMPTS ---
patterns = [
    r"\n    # 2026-07-28 hotfix: source-level hard-disable for daily-loss kill switch\..*?_safe_no_pause_state\(equity, config\)\n",
    r"\n\ndef _safe_no_pause_state\(\n\s*equity: float,\n\s*config: dict\[str, Any\],\n\s*\*,\n\s*existing: dict\[str, Any\] \| None = None,\n\s*\) -> dict\[str, Any\]:\n.*?return state\n",
    r"\n\ndef _safe_no_pause_state\(equity: float, config: dict\[str, Any\]\) -> dict\[str, Any\]:\n.*?return state\n",
]
for pat in patterns:
    new = re.sub(pat, "", src, flags=re.DOTALL)
    if new != src:
        print(f"stripped a prior insertion ({len(pat)} chars pattern)")
        src = new

# --- 2. LOCATE FUNCTION ---
m = re.search(r"def evaluate_daily_growth\(", src)
if not m:
    print("def evaluate_daily_growth not found")
    sys.exit(2)
start = m.start()

# --- 3. LOCATE DOCSTRING CLOSE ---
# The function docstring opens with `"""` on a line near the function start.
# Find the third `"""` (close).
quote_count = 0
search_pos = start
docstring_close_pos = None
while True:
    next_q = src.find('"""', search_pos)
    if next_q == -1:
        break
    quote_count += 1
    if quote_count == 2:
        # third quote would be the close; keep scanning
        pass
    if quote_count == 3:
        docstring_close_pos = next_q + 3
        break
    search_pos = next_q + 3
if not docstring_close_pos:
    print("couldn't find docstring close triple-quote")
    sys.exit(2)
print(f"function starts at byte {start}, docstring closes at byte {docstring_close_pos}")

# --- 4. INSERT GUARD ---
guard = (
    "\n\n    # 2026-07-28 hotfix: source-level HARD-DISABLE for the daily-loss kill\n"
    "    # switch. When practice.force_disable_max_loss_pause: true is set in any\n"
    "    # config layer, evaluate_daily_growth() never sets trading_paused=True\n"
    "    # or writes a pause_reason. This patch guards at the source so it survives\n"
    "    # config.yaml/profile merge semantics and any hot-reload gap.\n"
    "    if bool(\n"
    "        (config or {}).get(\"practice\", {}).get(\n"
    "            \"force_disable_max_loss_pause\", False\n"
    "        )\n"
    "    ):\n"
    "        return _safe_no_pause_state(equity, config, existing=existing)\n"
)
new_src = src[:docstring_close_pos] + guard + src[docstring_close_pos:]

# --- 5. APPEND HELPER (if not already present) ---
helper = (
    "\n\ndef _safe_no_pause_state(\n"
    "    equity: float,\n"
    "    config: dict[str, Any],\n"
    "    *,\n"
    "    existing: dict[str, Any] | None = None,\n"
    ") -> dict[str, Any]:\n"
    '    """Source-level hard-disable return shape (2026-07-28).\n\n'
    "    Reuses caller's pre-built state if provided; otherwise rolls forward\n"
    "    from sync_daily_session(). Forces trading_paused=False and\n"
    "    pause_reason=None so the daily-loss kill switch never trips.\n"
    '    """\n'
    "    if existing:\n"
    "        state = dict(existing)\n"
    "    else:\n"
    "        state = dict(sync_daily_session(equity, config))\n"
    '    state["trading_paused"] = False\n'
    '    state["pause_reason"] = None\n'
    '    state["updated_at"] = utc_now_iso()\n'
    "    return state\n"
)
if "_safe_no_pause_state" not in new_src:
    new_src = new_src.rstrip() + helper + "\n"
    print("appended helper")
else:
    print("helper already present")

open(TARGET, "w", encoding="utf-8").write(new_src)
print(f"wrote {TARGET}")

# --- 6. COMPILE CHECK ---
import py_compile

try:
    py_compile.compile(TARGET, doraise=True)
    print("PY_COMPILE: OK")
except py_compile.PyCompileError as e:
    print(f"PY_COMPILE FAILED: {e}")
    # dump lines 119..210 for diagnostic
    lines = open(TARGET, encoding="utf-8").read().split("\n")
    for i in range(119, min(len(lines), 220)):
        print(f"  {i + 1}: {lines[i]}")
    sys.exit(2)
