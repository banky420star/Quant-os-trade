"""Patch MT5Broker._place_order to send TP2 (NOT TP1) on the open order so
the broker doesn't pre-empt the bot's partial-TP code at TP1.

Why
---
Before this patch, `_place_order(...)` did:

    tp = float(signal["tp1"])

which attached a TP limit order at TP1 to every position open. Then the
broker fires a full close at TP1 BEFORE the bot's manage_partial_tp_mt5()
ever runs -- making the partial TP fallthrough (and the 0.01-lot
fallback hook I just added) effectively dead code.

After this patch
---------------
TP is set to TP2 (the runner target) at open time, so:

    broker limit TP order = TP2 -> does NOT fire at TP1
    bot 50% close at TP1 -> manage_partial_tp_mt5 fires
    broker limit TP order fires on remaining 50% at TP2

Net: 50% partial at TP1 + 50% runner at TP2 = exactly the partial-TP
semantics the user asked for.

If TP2 is missing from the signal (older callers), fall back to TP1 to
preserve previous behavior.
"""
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "core" / "mt5_broker.py"
src = P.read_text(encoding="utf-8")

# Replace the TP selection in _place_order. The line currently is:
#     sl = float(signal["sl"])
#     tp = float(signal["tp1"])
# and is in both the pending-order and market-order branches (each sends
# `tp` as a field). We replace the assignment once so it cascades for
# both branches.
old = (
    "        sl = float(signal[\"sl\"])\n"
    "        tp = float(signal[\"tp1\"])\n"
)
new = (
    "        sl = float(signal[\"sl\"])\n"
    "        # 2026-07-21 partial-TP fix: broker pre-empts the bot's\n"
    "        # partial TP at TP1 if we send TP1 as limit here. Send TP2\n"
    "        # so the bot can partial-close 50% at TP1, then the broker\n"
    "        # fires the remaining 50% at TP2 = exactly the partial-TP\n"
    "        # semantics. Falls back to TP1 if signal lacks tp2.\n"
    "        tp = float(signal.get(\"tp2\") or signal.get(\"tp1\") or 0)\n"
)
if old in src:
    src = src.replace(old, new, 1)
    P.write_text(src, encoding="utf-8")
    print("_place_order tp=t t2: applied")
else:
    print("_place_order tp=tp1: pattern not found (already patched?)")
