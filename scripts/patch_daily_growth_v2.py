"""Fix the broken patch in core/daily_growth.py.

The previous attempt inserted the guard above the `pause_reason =` line
which is inside an `elif` block. That put a top-of-function guard at
function-body indent level (8 spaces), but the elif scope is at 8-space
already - the inserted guard (still try 4-space) tangled the lexer.

Strategy:
1. Strip the broken insertion entirely.
2. Re-find `def evaluate_daily_growth(` in the file.
3. Insert the guard right after the docstring/closing triple-quote and
   the empty line that follows, at the function-body indent level (4 spaces).
4. Add the helper at the bottom (dedented).
5. py_compile. Verify.
"""
from __future__ import annotations
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "mt5_quant_agent", "core", "daily_growth.py")

src = open(TARGET, encoding="utf-8").read()

# Step 1: strip ALL previous hack attempts so we have a clean baseline.
# We remove the bad guard block AND the helper we appended.
patterns_to_remove = [
    r"\n    # 2026-07-28 hotfix: source-level hard-disable for daily-loss kill switch\..*?return _safe_no_pause_state\(equity, config\)\n",
    r"\n\ndef _safe_no_pause_state\(equity: float, config: dict\[str, Any\]\) -> dict\[str, Any\]:.*?return state\n",
    r"\n\ndef _safe_no_pause_state\(equity: float, config: dict\[str, Any\]\) -> dict\[str, Any\]:\n.*?return state\n",
]
for pat in patterns_to_remove:
    new_src = re.sub(pat, "", src, flags=re.DOTALL)
    if new_src != src:
        print(f"stripped pattern of length {len(pat)}")
        src = new_src

# Step 2: locate the def + docstring. Insert guard immediately AFTER the
# closing triple-quote of the docstring (assume docstring is ""..."" and
# the def signature is right after).
m = re.search(
    r'(def evaluate_daily_growth\([^)]*\)\s*->\s*dict\[str, Any\]:\s*\n\s*"""[^"]*"""\n)',
    src,
    flags=re.DOTALL,
)
if not m:
    print("Could not locate function definition with docstring. Aborting.")
    sys.exit(2)
insert_pos = m.end()

guard_block = (
    "    # 2026-07-28 hotfix: source-level HARD-DISABLE for the daily-loss kill\n"
    "    # switch. When practice.force_disable_max_loss_pause: true is set in any\n"
    "    # config layer, evaluate_daily_growth() never sets trading_paused=True\n"
    "    # or writes a pause_reason. This is the only patch that survives\n"
    "    # config.yaml/profile merge semantics because it guards at the source.\n"
    "    if bool(config.get(\"practice\", {}).get(\"force_disable_max_loss_pause\", False)):\n"
    "        return _safe_no_pause_state(equity, config, existing=existing)\n"
)
new_src = src[:insert_pos] + guard_block + src[insert_pos:]

# Step 3: append helper at end (after the last newline trailing whitespace).
helper = (
    "\n\ndef _safe_no_pause_state(\n"
    "    equity: float,\n"
    "    config: dict[str, Any],\n"
    "    *,\n"
    "    existing: dict[str, Any] | None = None,\n"
    ") -> dict[str, Any]:\n"
    '    """Source-level hard-disable return shape (2026-07-28).\n\n'
    "    Reuses the caller's pre-built state if provided (preserves any prior\n"
    "    day-start equity / target-pct / etc.); only forces trading_paused=False\n"
    "    and pause_reason=None so the daily-loss kill switch never fires.\n"
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
new_src = new_src.rstrip() + helper + "\n"

open(TARGET, "w", encoding="utf-8").write(new_src)
print("Source-level patch landed (correct indent + helper appended).")

# Step 4: compile check
import py_compile

try:
    py_compile.compile(TARGET, doraise=True)
    print("PY_COMPILE: OK")
except py_compile.PyCompileError as e:
    print(f"PY_COMPILE FAILED: {e}")
    # print the offending line ranges for diagnostic
    bad_src = open(TARGET, encoding="utf-8").read().split("\n")
    for ln in (181, 182, 183, 184):
        print(f"  {ln}: {bad_src[ln - 1] if ln - 1 < len(bad_src) else '<eof>'}")
    sys.exit(2)
