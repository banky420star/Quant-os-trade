"""purgedcv adoption audit (upgrade #1 from the researched-upgrades plan).

GOAL
  Use the `purgedcv` library (Lazarev 2026, https://github.com/eslazarev/purged-cross-
  validation) as an INDEPENDENT engine for PurgedKFold + embargo + WalkForwardSplit
  + CPCV + PBO + DSR, to confirm the de-leaked quantum-loop verdict from a second
  implementation. This is "verify-before-trust": purgedcv is v0.1.2 / 14 stars, so we
  sanity-check its PBO on a known synthetic random walk (expect PBO ~= 0.5) BEFORE
  trusting its output, and we do NOT wire it into the main quantum_loop grid path --
  it runs as a standalone audit.

WHY NOT WIRED INTO quantum_loop.py
  The main grid uses scripts.strategy_evaluator.make_splits (homegrown expanding
  walk-forward) and the in-house deflated_sharpe. Replacing either with an
  unverified 14-star dependency would risk the integrity of the verdict. Once this
  audit corroborates the verdict AND purgedcv's synthetic PBO checks out, a follow-
  up can wire it in behind a flag.

PREREQ (BLOCKED on a pip install the auto-classifier denied)
  pip install purgedcv   # then re-run this script
  If the import below fails, the script prints the install instructions and exits.

Read-only. No live trades.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _try_import():
    try:
        import purgedcv  # type: ignore
        return purgedcv, None
    except Exception as e:  # pragma: no cover
        return None, str(e)


def synthetic_pbo_sanity(purgedcv, *, n_strat: int = 50, t: int = 500, seed: int = 20260626):
    """Random-walk strategies: PBO should be ~0.5 (no real edge => best-IS is
    no better than a coin flip OOS). If purgedcv returns PBO far from 0.5 here,
    its math is suspect and its real-data output cannot be trusted."""
    rng = random.Random(seed)
    # N independent length-T random walks (cumulative sum of iid N(0,1) per-trade R)
    matrix = []
    for _ in range(n_strat):
        rs = [rng.gauss(0, 1) for _ in range(t)]
        matrix.append(rs)
    # Try the library's PBO entrypoints (names may vary by version).
    for fn_name in ("pbo", "PBO", "probability_of_backtest_overfitting", "compute_pbo"):
        fn = getattr(purgedcv, fn_name, None)
        if fn is not None:
            try:
                res = fn(matrix)
                pbo = res if isinstance(res, (int, float)) else getattr(res, "pbo", getattr(res, "PBO", None))
                return float(pbo) if pbo is not None else None, fn_name
            except Exception:
                continue
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser(description="purgedcv independent CPCV/PBO/DSR audit")
    ap.add_argument("--dump", type=Path, default=ROOT / "deleaked_rseries.json")
    ap.add_argument("--skip-sanity", action="store_true")
    args = ap.parse_args()

    purgedcv, err = _try_import()
    if purgedcv is None:
        print("purgedcv is NOT installed. Install it with:")
        print("  ! pip install purgedcv")
        print("(This was denied by the Claude Code auto-classifier as an agent-chosen")
        print(" package; the user must authorize it or add a Bash permission rule.)")
        print(f"\nImport error: {err}")
        return 2

    print(f"purgedcv imported: {getattr(purgedcv, '__version__', '?')}")
    # 1) Sanity check on synthetic random walk -- VERIFY BEFORE TRUST.
    if not args.skip_sanity:
        pbo, fn = synthetic_pbo_sanity(purgedcv)
        print(f"[sanity] synthetic random-walk PBO via purgedcv.{fn} = {pbo}")
        if pbo is not None and 0.35 <= pbo <= 0.65:
            print("[sanity] OK -- PBO near 0.5 as expected; purgedcv math is plausible.")
        elif pbo is not None:
            print("[sanity] WARNING -- PBO far from 0.5 on pure noise; DO NOT trust "
                  "purgedcv's real-data PBO. Vendor-verify its math vs Bailey et al. 2015.")
            return 3
        else:
            print("[sanity] Could not call any purgedcv PBO entrypoint (API changed?). "
                  "Inspect purgedcv.__dict__ and update this script.")
            return 3

    # 2) Real-data PBO + DSR on the de-leaked R-series.
    if not args.dump.exists():
        print(f"\nNo dump at {args.dump}. Re-run quantum_loop.py with DUMP_RSERIES={args.dump}.")
        return 2
    data = json.loads(args.dump.read_text())
    rseries = data.get("r_series_net") or data.get("r_series") or {}
    kept = [list(map(float, v)) for v in rseries.values() if len(v) >= 40]
    if len(kept) < 2:
        print(f"Only {len(kept)} cells with >=40 trades in the dump; need >=2.")
        return 2
    pbo, fn = synthetic_pbo_sanity(purgedcv) if False else (None, None)  # noqa
    # Reuse the entrypoint found in sanity; call on real matrix.
    for fn_name in ("pbo", "PBO", "probability_of_backtest_overfitting", "compute_pbo"):
        fn = getattr(purgedcv, fn_name, None)
        if fn is not None:
            try:
                res = fn(kept)
                pbo = res if isinstance(res, (int, float)) else getattr(res, "pbo", getattr(res, "PBO", None))
                break
            except Exception:
                continue
    print(f"\n[real data] de-leaked grid PBO via purgedcv = {pbo} (N={len(kept)} cells)")
    print("Cross-check vs scripts/validation_audit.py (hand-rolled CSCV PBO).")
    print("If both agree the grid is overfit (PBO>0.5) and DSR<0.95 for all cells,")
    print("the no-deploy verdict is corroborated by two independent engines + the")
    print("library the research agent recommended.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())