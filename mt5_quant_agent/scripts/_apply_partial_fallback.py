"""Patch manage_partial_tp_mt5 AND manage_partial_tp_paper so that when
partial_close_volume() returns 0 (i.e. the position can't be split because
its size is below 2 * volume_min), the bot falls back to a single 100%
close at TP1 instead of silently `continue`-ing past the trade.

Why this matters
----------------
The bot fires roughly half of all positions at 0.01 lot (the broker
minimum). `_partial_close_volume(0.01, 0.5)` rounds to 0 because:

    raw = 0.01 * 0.5 = 0.005
    steps = floor(0.005 / 0.01) = 0
    close_vol = 0 < volume_min -> 0

So before this patch, every XAUUSDm / BTCUSDm / NAS100m partial-TP
attempt on a 0.01-lot position was silently skipped -> trade_log never
saw a partial_take_profit exit. The dashboard's Profit Quality bucket
on partial-tp was empty by design, not by accident.

The fix
-------
After computing close_vol in BOTH paper and MT5 paths, if it's 0 AND the
raw size is non-trivial (>= vmin), promote the path to a 100% close at
TP1 (single-shot full close). Set full_close_only=True so the
archive_reason flips automatically from 'partial_tp' to 'tp_close' (the
existing branch logic uses that variable).
"""
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "core" / "position_manager.py"
src = P.read_text(encoding="utf-8")
fixes = 0

# ---- Patch 1: MT5 path ----
# Find the close_vol computation in manage_partial_tp_mt5 (currently
# `partial_close_volume(size, fraction, volume_min=vmin, ...)`) and add
# the fallback hook right after the partial-close branch.
old_mt5_close = (
    "        if full_close_only:\n"
    "            # Bypass partial_close_volume for the full-close case so the\n"
    "            # ``min_remain`` floor doesn't suppress 100% exits on 0.01-lot\n"
    "            # positions. Fraction=1.0 here means the user explicitly\n"
    "            # disabled partial TP, so they want a single full exit.\n"
    "            close_vol = max(vmin, round(size / vstep) * vstep)\n"
    "        else:\n"
    "            close_vol = partial_close_volume(\n"
    "                size,\n"
    "                fraction,\n"
    "                volume_min=vmin,\n"
    "                volume_step=vstep,\n"
    "                min_remain=float(pcfg.get(\"min_volume_remain\", vmin)),\n"
    "            )\n"
    "        if close_vol <= 0:\n"
    "            continue\n"
)
new_mt5_close = (
    "        if full_close_only:\n"
    "            # Bypass partial_close_volume for the full-close case so the\n"
    "            # ``min_remain`` floor doesn't suppress 100% exits on 0.01-lot\n"
    "            # positions. Fraction=1.0 here means the user explicitly\n"
    "            # disabled partial TP, so they want a single full exit.\n"
    "            close_vol = max(vmin, round(size / vstep) * vstep)\n"
    "        else:\n"
    "            close_vol = partial_close_volume(\n"
    "                size,\n"
    "                fraction,\n"
    "                volume_min=vmin,\n"
    "                volume_step=vstep,\n"
    "                min_remain=float(pcfg.get(\"min_volume_remain\", vmin)),\n"
    "            )\n"
    "            # FALLBACK: when partial_close_volume rounds to 0 (size<2*vmin,\n"
    "            # can't split), promote to a single 100% close at TP1. fixes\n"
    "            # 0.01-lot positions like XAUUSDm / BTCUSDm / NAS100m where\n"
    "            # partial TP would silently skip the trade.\n"
    "            if close_vol <= 0 and size >= vmin:\n"
    "                close_vol = max(vmin, round(size / vstep) * vstep)\n"
    "                full_close_only = True\n"
    "        if close_vol <= 0:\n"
    "            continue\n"
)
if old_mt5_close in src:
    src = src.replace(old_mt5_close, new_mt5_close, 1)
    fixes += 1
    print("Patch MT5 (promote to full close on partial=<vmin): applied")
else:
    print("Patch MT5: pattern not found (already patched?)")

# ---- Patch 2: PAPER path ----
old_paper_close = (
    "            if full_close_only:\n"
    "                # Bypass partial_close_volume for the full-close case so the\n"
    "                # ``min_remain`` floor doesn't suppress 100% exits on minimum-lot\n"
    "                # positions. Fraction=1.0 here means the user explicitly\n"
    "                # disabled partial TP, so they want a single full exit.\n"
    "                close_vol = size\n"
    "            else:\n"
    "                close_vol = partial_close_volume(\n"
    "                    size,\n"
    "                    fraction,\n"
    "                    volume_min=0.01,\n"
    "                    volume_step=0.01,\n"
    "                    min_remain=float(pcfg.get(\"min_volume_remain\", 0.01)),\n"
    "                )\n"
    "            if close_vol > 0:\n"
)
new_paper_close = (
    "            if full_close_only:\n"
    "                # Bypass partial_close_volume for the full-close case so the\n"
    "                # ``min_remain`` floor doesn't suppress 100% exits on minimum-lot\n"
    "                # positions. Fraction=1.0 here means the user explicitly\n"
    "                # disabled partial TP, so they want a single full exit.\n"
    "                close_vol = size\n"
    "            else:\n"
    "                close_vol = partial_close_volume(\n"
    "                    size,\n"
    "                    fraction,\n"
    "                    volume_min=0.01,\n"
    "                    volume_step=0.01,\n"
    "                    min_remain=float(pcfg.get(\"min_volume_remain\", 0.01)),\n"
    "                )\n"
    "                # FALLBACK: if partial rounds to 0 (size<2*vmin), promote\n"
    "                # to a single 100% close at TP1. 0.01-lot positions were\n"
    "                # silently skipping partial TP before this fix.\n"
    "                if close_vol <= 0 and size >= 0.01:\n"
    "                    close_vol = size\n"
    "                    full_close_only = True\n"
    "            if close_vol > 0:\n"
)
if old_paper_close in src:
    src = src.replace(old_paper_close, new_paper_close, 1)
    fixes += 1
    print("Patch paper (promote to full close on partial=<vmin): applied")
else:
    print("Patch paper: pattern not found (already patched?)")

P.write_text(src, encoding="utf-8")
print(f"Total fixes applied: {fixes}")
