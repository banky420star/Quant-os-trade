"""Apply two reviewer-flagged fixes to position_manager.py.

1) Guard `current <= 0` in `_exit_trigger_met_pct` so a corrupt 0 tick
   cannot phantom-fire BE on a SELL.
2) Honour `usd_lock_raw` on the percent-trigger BE branch so an explicit
   `lock_profit_usd: 0.20` buffer still applies when the percent path fires.
"""
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "core" / "position_manager.py"
src = P.read_text(encoding="utf-8")
fixes = 0

# Fix 1: add current <= 0 guard
old1 = "    if trigger_pct <= 0 or entry <= 0:\n        return False\n"
new1 = "    if trigger_pct <= 0 or entry <= 0 or current <= 0:\n        return False\n"
if old1 in src:
    src = src.replace(old1, new1, 1)
    fixes += 1
    print("Fix 1 (current<=0 guard): applied")
else:
    print("Fix 1: pattern not found or already applied")

# Fix 2: percent-path honours usd_lock_raw
old2 = (
    "                if lock_pts is not None:\n"
    "                    lock = lock_pts\n"
    "                else:\n"
    "                    lock = 0.0\n"
)
new2 = (
    "                if lock_pts is not None:\n"
    "                    lock = lock_pts\n"
    "                elif usd_triggered and usd_lock_raw is not None:\n"
    "                    lock = max(0.0, float(usd_lock_raw))\n"
    "                else:\n"
    "                    lock = 0.0\n"
)
if old2 in src:
    # Replace only the first occurrence (under the be_pct_hit branch)
    src = src.replace(old2, new2, 1)
    fixes += 1
    print("Fix 2 (honour usd_lock_raw on percent path): applied")
else:
    print("Fix 2: pattern not found or already applied")

P.write_text(src, encoding="utf-8")
print(f"Total fixes applied: {fixes}")
