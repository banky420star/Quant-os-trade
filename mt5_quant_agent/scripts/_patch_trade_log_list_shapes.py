"""Patch the LIST/DICT shape mismatch across 5 call sites that crash on
'list' object has no attribute 'get'.

Each fix is the same minimal pattern: inline `isinstance(data, dict)` guard
around any downstream .get() call. The dashboard /api/profit_quality endpoint
already has this pattern (server.py); we're extending it to the loops and
core helpers that read trade_log.json / paper_trades.json.

Idempotent — re-running doesn't break anything.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXES: list[tuple[str, str, str]] = [
    # (file, original_substring_with_max_1_occurrence_marker, replacement)
    (
        "loops/evaluation_loop.py",
        'recent_trades=list(trades.get("trades") or []),',
        'recent_trades=list(trades if isinstance(trades, list) else (trades or {}).get("trades") or []),',
    ),
    (
        "loops/learning_review_loop.py",
        'tl = read_json_state("trade_log.json", default={}) or {}\n    trades = list(tl.get("trades") or [])',
        'tl = read_json_state("trade_log.json", default=[]) or []\n'
        '    trades = list(tl if isinstance(tl, list) else (tl or {}).get("trades") or [])',
    ),
    (
        "loops/trade_log_loop.py",
        'prev = read_json_state("trade_log.json", default={}) or {}\n'
        '    last_trades_m = float(prev.get("_paper_trades_mtime", 0.0) or 0.0)\n'
        '    last_orders_m = float(prev.get("_paper_orders_mtime", 0.0) or 0.0)',
        'prev = read_json_state("trade_log.json", default={}) or {}\n'
        '    _prev_dict = prev if isinstance(prev, dict) else {}\n'
        '    last_trades_m = float(_prev_dict.get("_paper_trades_mtime", 0.0) or 0.0)\n'
        '    last_orders_m = float(_prev_dict.get("_paper_orders_mtime", 0.0) or 0.0)',
    ),
    (
        "core/adaptive_gates.py",
        'tl = read_json_state("trade_log.json", default={}) or {}\n'
        '    trades = list(tl.get("trades") or [])',
        'tl = read_json_state("trade_log.json", default=[]) or []\n'
        '    trades = list(tl if isinstance(tl, list) else (tl or {}).get("trades") or [])',
    ),
    (
        "core/regime_evolution.py",
        'tl = read_json_state("trade_log.json", default={}) or {}\n'
        '    trades = list(tl.get("trades") or [])',
        'tl = read_json_state("trade_log.json", default=[]) or []\n'
        '    trades = list(tl if isinstance(tl, list) else (tl or {}).get("trades") or [])',
    ),
]


def main() -> int:
    n_patched = 0
    n_skipped = 0
    for rel, old, new in FIXES:
        p = ROOT / rel
        src = p.read_text(encoding="utf-8", errors="replace")
        if old not in src:
            print(f"  SKIP {rel}: pattern not found (already patched or text drifted)")
            n_skipped += 1
            continue
        count = src.count(old)
        if count > 1:
            print(f"  WARN {rel}: pattern matched {count} times — applying once only")
        patched = src.replace(old, new, 1)
        p.write_text(patched, encoding="utf-8")
        print(f"  PATCH {rel}")
        n_patched += 1
    print(f"\nApplied {n_patched} patches, skipped {n_skipped} (already-applied).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
