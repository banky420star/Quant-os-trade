"""One-shot fix: recompute the stale `dsr_crude_gross` field in
quantum_loop_report_deleaked2.json from the dumped GROSS R-series.

WHY
  The on-disk report predates the quantum_loop.py:566 fix that pointed the
  `dsr_crude_gross` diff field at the GROSS series (all_r_series_gross). The stale
  JSON instead computed crude-threshold DSR on the NET series, yielding 0.1435 for
  the best cell where the corrected code yields 0.7152. VERDICT.md and the current
  code both report ~0.715; only the JSON artifact was stale.

  Rather than re-run the 9-cell x 3-fold grid (hours), recompute the one field from
  the already-dumped per-trade gross R series + the across-cell SR variance, using
  the same `deflated_sharpe` call the live code makes. Idempotent and read-only on
  everything except the single JSON field.

Usage
  python scripts/fix_stale_dsr.py [--report quantum_loop_report_deleaked2.json]
                                 [--dump deleaked_rseries.json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Import the exact function the grid uses, so the patched field is byte-for-byte
# consistent with a fresh run.
import sys
sys.path.insert(0, str(ROOT / "scripts"))
from quantum_loop import deflated_sharpe  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path,
                    default=ROOT / "quantum_loop_report_deleaked2.json")
    ap.add_argument("--dump", type=Path, default=ROOT / "deleaked_rseries.json")
    args = ap.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    dump = json.loads(args.dump.read_text(encoding="utf-8"))
    gross = dump.get("r_series_gross") or {}
    K = len(report.get("ranked", []))
    if K <= 1:
        print(f"K={K} -- nothing to deflate; aborting.")
        return 2
    if not gross:
        print("r_series_gross missing from dump; cannot recompute.")
        return 2

    # The old convention (crude threshold on GROSS R) used a GROSS-based cross-trial
    # SR variance, so reproduce it faithfully: compute per-cell gross Sharpe from the
    # dumped r_series_gross, then its variance. (The net-based dump sr_var would
    # understate selection bias and overstate the diff field.)
    def _sr(rs):
        if len(rs) < 2:
            return 0.0
        m = sum(rs) / len(rs)
        v = sum((x - m) ** 2 for x in rs) / len(rs)
        return m / (v ** 0.5) if v > 0 else 0.0
    if dump.get("sr_var_gross") is not None:
        sr_var_gross = float(dump["sr_var_gross"])
        src = "dump"
    else:
        cs = {k: _sr([float(x) for x in v]) for k, v in gross.items()}
        vals = list(cs.values())
        sr_var_gross = sum((x - sum(vals) / len(vals)) ** 2 for x in vals) / len(vals) if vals else 0.0
        src = "recomputed from r_series_gross"

    changed = 0
    for r in report.get("ranked", []):
        cell = r["cell"]
        rs = gross.get(cell, [])
        if not rs:
            print(f"  WARN: no gross series for {cell}; leaving field unchanged")
            continue
        corrected = round(deflated_sharpe([float(x) for x in rs], n_trials=K,
                                           sr_var_across_trials=sr_var_gross,
                                           threshold_mode="crude"), 4)
        old = r.get("dsr_crude_gross")
        r["dsr_crude_gross"] = corrected
        if old != corrected:
            changed += 1
            print(f"  {cell}: dsr_crude_gross {old} -> {corrected}")

    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nDone. {changed} field(s) corrected in {args.report.name} (K={K}, "
          f"sr_var_gross={sr_var_gross:.4f} [{src}]).")
    print("dsr_precise (the gate) was NOT touched -- it was already correct.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())