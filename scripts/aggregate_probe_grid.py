"""Aggregate per-strategy probe_grid JSON into master_summary."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRID = ROOT / "state" / "probe_grid"


def main() -> None:
    all_rows: list[dict] = []
    for path in sorted(GRID.glob("*.json")):
        if path.stem == "master_summary":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for r in data.get("all_results", []):
            r = dict(r)
            r["strategy"] = data.get("strategy", path.stem)
            all_rows.append(r)

    symbols = sorted({r["symbol"] for r in all_rows})
    per_sym = []
    for sym in symbols:
        rows = [r for r in all_rows if r["symbol"] == sym]
        per_sym.append(max(rows, key=lambda x: (x.get("pass", False), x.get("score", -999))))

    top = sorted(all_rows, key=lambda x: (x.get("pass", False), x.get("score", -999)), reverse=True)[:15]
    near = [
        r for r in all_rows
        if r.get("expectancy_r", 0) >= 0.05 and str(r.get("fold_positive", "")) in ("2/3", "3/3")
    ]
    near = sorted(near, key=lambda x: x.get("expectancy_r", 0), reverse=True)[:20]
    passed = [r for r in all_rows if r.get("pass")]

    report = {
        "total_cells": len(all_rows),
        "passed_cells": len(passed),
        "variants_tested": ["base", "rr2", "rr2_sl1", "tight_session", "rr2_tight", "strict_rr2"],
        "per_symbol_best": {r["symbol"]: r for r in per_sym},
        "top15_overall": top,
        "near_miss": near,
        "integrate_any": len(passed) > 0,
    }
    out = GRID / "master_summary.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(f"Cells: {len(all_rows)}  PASS: {len(passed)}")
    print("\n=== PER SYMBOL BEST ===")
    for r in per_sym:
        flag = "PASS" if r.get("pass") else "FAIL"
        print(
            f"  {r['symbol']:<10} {r['strategy']:<30} {r['variant']:<14} "
            f"exp={r.get('expectancy_r', 0):+.3f}R wr={r.get('win_rate_pct', 0)}% "
            f"n={r.get('n_trades', 0)} folds={r.get('fold_positive')} {flag}"
        )
    print("\n=== TOP 10 OVERALL ===")
    for i, r in enumerate(top[:10], 1):
        print(
            f"  {i:2}. {r['symbol']:<10} {r['strategy']:<30} {r['variant']:<14} "
            f"exp={r.get('expectancy_r', 0):+.3f}R n={r.get('n_trades', 0)} folds={r.get('fold_positive')}"
        )
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()