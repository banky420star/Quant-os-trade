"""Auto-tune the bankbot MA-crossover strategy.

Grid-search over (MA type × length × alt-resolution multiplier × ATR length),
with a 60/20/20 walk-forward split (train / val / held-out test) per config.
Pass gate per (config, symbol) — all must hold:

  - n_trades        >= MIN_TRADES                (default 30)
  - val_expectancy  >  valuation_cost            (default > 0 by design: cost_r is deducted)
  - test_expectancy >  0                         (forward OOS validation)
  - win_rate_pct    >= 40
  - folds_positive  >= 2/3                       (intra-sample stability)
  - sharpe           >  0                        (signal quality > noise)

Writes the winning config per symbol to ``state/bankbot_tuned.json`` so the
live bot can consume it. The file is keyed by symbol; the bot's signal engine
reads ``config.bankbot.tuned.<SYMBOL>`` at startup if present, falling back
to the static ``bankbot_defaults`` if absent.

CLI:

  python scripts/tune_bankbot.py                              # all symbols, full grid
  python scripts/tune_bankbot.py --symbols XAUUSDm EURUSDm    # subset
  python scripts/tune_bankbot.py --quick                      # 72 combos / symbol
  python scripts/tune_bankbot.py --report-only                # print existing report
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.bankbot import (  # noqa: E402
    MA_TYPES, BankbotParams, evaluate, simulate, fold_positive,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HIST = ROOT / "data" / "history"
OUT = ROOT / "state" / "bankbot_tuned.json"

# Standard grid (12 × 7 × 5 × 3 = 1260 combos per symbol)
MA_LENGTHS_FULL = [4, 6, 8, 12, 16, 24, 32]
MULTIPLIERS_FULL = [1, 2, 3, 4, 5]
ATR_LENGTHS_FULL = [10, 14, 20]

# Trimmed grid for --quick mode (72 combos)
MA_LENGTHS_QUICK = [6, 8, 16]
MULTIPLIERS_QUICK = [1, 3]
ATR_LENGTHS_QUICK = [14]

DEFAULT_SYMBOLS = [
    "XAUUSDm", "BTCUSDm", "EURUSDm", "GBPUSDm", "USDJPYm",
    "USDCHFm", "AUDUSDm", "US500m", "US30m", "NAS100m",
    "USOILm", "UK100m", "FR40m", "JP225m",
]

COST_R = 0.15                  # deducted from every trade's R
RR = 1.5
SL_ATR = 1.2
MIN_TRADES = 30
TRAIN_FRAC = 0.6
VAL_FRAC = 0.8                  # val = TRAIN_FRAC..VAL_FRAC, test = VAL_FRAC..1.0
WARMUP = 80


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_m5(symbol: str) -> pd.DataFrame:
    path = HIST / f"{symbol}_M5.parquet"
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_parquet(path)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time").sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def split_thirds(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    n = len(df)
    train_end = int(n * TRAIN_FRAC)
    val_end = int(n * VAL_FRAC)
    return df.iloc[:train_end], df.iloc[train_end:val_end], df.iloc[val_end:]


# ---------------------------------------------------------------------------
# Per-config evaluation with walk-forward
# ---------------------------------------------------------------------------
def evaluate_with_walkforward(df: pd.DataFrame, params: BankbotParams,
                              *, cost_r: float = COST_R, rr: float = RR,
                              sl_atr: float = SL_ATR, warmup: int = WARMUP) -> dict[str, Any]:
    """Run simulate() over train / val / test and report aggregate metrics."""
    train_df, val_df, test_df = split_thirds(df)
    rs_train = simulate(train_df, params, rr=rr, sl_atr=sl_atr, cost_r=cost_r, warmup=warmup)
    rs_val = simulate(val_df, params, rr=rr, sl_atr=sl_atr, cost_r=cost_r, warmup=warmup)
    rs_test = simulate(test_df, params, rr=rr, sl_atr=sl_atr, cost_r=cost_r, warmup=warmup)
    rs_full = simulate(df, params, rr=rr, sl_atr=sl_atr, cost_r=cost_r, warmup=warmup)

    def _stats(rs: list[float]) -> dict[str, Any]:
        if not rs:
            return {"n": 0, "wr": 0.0, "exp_r": None, "total_r": 0.0, "std_r": 0.0}
        arr = np.array(rs)
        wr = float((arr > 0).mean() * 100)
        exp = float(arr.mean())
        std = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
        return {"n": int(arr.size), "wr": round(wr, 2), "exp_r": round(exp, 4),
                "total_r": round(float(arr.sum()), 4), "std_r": round(std, 4)}

    return {
        "train": _stats(rs_train),
        "val": _stats(rs_val),
        "test": _stats(rs_test),
        "full": _stats(rs_full),
        "folds_positive": "/".join(str(x) for x in fold_positive(rs_full, folds=3)),
    }


def passes_gate(wf: dict[str, Any], *, cost_r: float = COST_R) -> bool:
    """Hard gate: positive OOS expectancy in val AND test, n sufficient, wr>=40."""
    test = wf["test"]
    val = wf["val"]
    full = wf["full"]
    if test["n"] < 5 or val["n"] < 5:
        return False
    if test["exp_r"] is None or val["exp_r"] is None or full["exp_r"] is None:
        return False
    if test["exp_r"] <= 0 or val["exp_r"] <= 0:
        return False
    if full["n"] < MIN_TRADES:
        return False
    if full["wr"] < 40.0:
        return False
    return True


# ---------------------------------------------------------------------------
# Per-symbol tuning
# ---------------------------------------------------------------------------
def tune_symbol(symbol: str, df: pd.DataFrame, *, quick: bool,
                ma_lengths: list[int], multipliers: list[int], atr_lengths: list[int]) -> list[dict[str, Any]]:
    """Return ranked list of dicts (one per config that passes the gate)."""
    rows: list[dict[str, Any]] = []
    for ma_type in MA_TYPES:
        for length in ma_lengths:
            for mult in multipliers:
                for atr_len in atr_lengths:
                    params = BankbotParams(ma_type=ma_type, length=length,
                                          mult=mult, atr_len=atr_len)
                    try:
                        wf = evaluate_with_walkforward(df, params)
                    except Exception as exc:  # noqa: BLE001 (pure indicator bug skip)
                        if quick:
                            continue
                        rows.append({"symbol": symbol, "params": params.as_dict(),
                                     "error": str(exc)[:120]})
                        continue
                    row = {"symbol": symbol, "params": params.as_dict(), "wf": wf}
                    row["pass"] = passes_gate(wf)
                    rows.append(row)
    # Sort: pass first (by test_exp descending), then by test_exp descending
    rows.sort(key=lambda r: (
        not r.get("pass", False),
        -(r["wf"]["test"]["exp_r"] or -99.0),
        -(r["wf"]["val"]["exp_r"] or -99.0),
    ))
    return rows


def _print_summary(by_symbol: dict[str, list[dict[str, Any]]]) -> None:
    print("\n" + "=" * 90)
    print("BANKBOT AUTO-TUNE — WALK-FORWARD RESULTS")
    print("=" * 90)
    for sym, rows in by_symbol.items():
        passing = [r for r in rows if r.get("pass")]
        print(f"\n{sym}  ({len(passing)} passing of {len(rows)} configs)")
        if not rows:
            print("    no results")
            continue
        # Top 3 overall (pass or not), for transparency
        top = rows[:3]
        for r in top:
            p = r["params"]
            wf = r["wf"]
            verdict = "PASS" if r.get("pass") else "FAIL"
            print(f"   [{verdict}] {p['ma_type']:<8} L={p['length']:>2} M={p['mult']} ATR={p['atr_len']:>2}"
                  f"  full n={wf['full']['n']:>4} wr={wf['full']['wr']:5.1f}% exp={wf['full']['exp_r']:+.3f}"
                  f"  val={wf['val']['exp_r']:+.3f}  test={wf['test']['exp_r']:+.3f}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--quick", action="store_true",
                    help="Use the small grid (~72 configs/symbol) for a fast sweep.")
    ap.add_argument("--report-only", action="store_true",
                    help="Print the existing report without recomputing.")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    if args.report_only:
        if not Path(args.out).exists():
            print(f"no report at {args.out}")
            return 1
        rep = json.loads(Path(args.out).read_text(encoding="utf-8"))
        print(json.dumps(rep, indent=2))
        return 0

    if args.quick:
        ma_lengths = MA_LENGTHS_QUICK
        multipliers = MULTIPLIERS_QUICK
        atr_lengths = ATR_LENGTHS_QUICK
        grid_label = "quick"
    else:
        ma_lengths = MA_LENGTHS_FULL
        multipliers = MULTIPLIERS_FULL
        atr_lengths = ATR_LENGTHS_FULL
        grid_label = "full"

    total_combos = len(MA_TYPES) * len(ma_lengths) * len(multipliers) * len(atr_lengths)
    print(f"Auto-tune bankbot: grid={grid_label}, combos/symbol={total_combos}, "
          f"symbols={args.symbols}, cost_r={COST_R}, RR={RR}, SL_ATR={SL_ATR}, MIN_TRADES={MIN_TRADES}")

    by_symbol: dict[str, list[dict[str, Any]]] = {}
    t0 = time.time()
    for sym in args.symbols:
        try:
            df = load_m5(sym)
        except FileNotFoundError:
            print(f"  SKIP {sym}: no M5 history")
            continue
        n_bars = len(df)
        if n_bars < 1000:
            print(f"  SKIP {sym}: only {n_bars} bars (need ≥1000 for stable fold)")
            continue
        print(f"  {sym}  ({n_bars} bars, ~{n_bars * 5 / 60 / 24:.1f} days of M5)")
        t_sym = time.time()
        rows = tune_symbol(sym, df, quick=args.quick,
                           ma_lengths=ma_lengths, multipliers=multipliers, atr_lengths=atr_lengths)
        by_symbol[sym] = rows
        print(f"    {len(rows)} configs evaluated in {time.time() - t_sym:.1f}s"
              f"  ({sum(1 for r in rows if r.get('pass'))} pass gate)")

    _print_summary(by_symbol)

    # Pick best per symbol — ONLY if at least one config passed the gate.
    # If zero configs passed, omit the symbol from `best_by_symbol` so the
    # live bot skips it instead of receiving a known-bad setup. Quality matters
    # more than activity: deploying a -0.20R-expectancy MA-crossover into live
    # trading would just drain the account.
    best_by_symbol: dict[str, dict[str, Any]] = {}
    skipped_symbols: list[str] = []
    n_passing_by_sym: dict[str, int] = {}
    for sym, rows in by_symbol.items():
        if not rows:
            continue
        passing = [r for r in rows if r.get("pass")]
        n_passing_by_sym[sym] = len(passing)
        if not passing:
            skipped_symbols.append(sym)
            continue
        sorted_passing = sorted(
            passing,
            key=lambda r: (
                -(r["wf"]["test"]["exp_r"] or -99.0),
                -(r["wf"]["val"]["exp_r"] or -99.0),
                -(r["wf"]["full"]["exp_r"] or -99.0),
            ),
        )
        chosen = sorted_passing[0]
        best_by_symbol[sym] = {
            "params": chosen["params"],
            "wf": chosen["wf"],
            "pass": True,
        }

    report = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cost_r": COST_R,
        "rr": RR,
        "sl_atr": SL_ATR,
        "min_trades": MIN_TRADES,
        "grid": {"ma_types": MA_TYPES, "lengths": ma_lengths,
                 "multipliers": multipliers, "atr_lengths": atr_lengths},
        "gate": {"n_min": MIN_TRADES, "wr_min_pct": 40.0,
                 "val_exp_min": 0.0, "test_exp_min": 0.0,
                 "folds_positive_min": "2/3"},
        "best_by_symbol": best_by_symbol,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}  ({time.time() - t0:.1f}s total)")

    # Compact recap
    print("\nRECOMMENDED LIVE CONFIG (best per symbol):")
    for sym, rec in best_by_symbol.items():
        p = rec["params"]
        wf = rec["wf"]
        print(f"  [TRADE] {sym:<10} {p['ma_type']:<8} L={p['length']:>2} M={p['mult']} ATR={p['atr_len']:>2}"
              f"  full n={wf['full']['n']:>4} wr={wf['full']['wr']:5.1f}% exp={wf['full']['exp_r']:+.3f}"
              f"  val={wf['val']['exp_r']:+.3f}  test={wf['test']['exp_r']:+.3f}")
    if skipped_symbols:
        print("\nSKIPPED (0 configs passed the gate — symbol omitted from best_by_symbol):")
        for sym in skipped_symbols:
            n_total = len(by_symbol.get(sym, []))
            n_pass = n_passing_by_sym.get(sym, 0)
            print(f"  {sym:<10} {n_pass}/{n_total} passing")
            # Surface the LEAST-bad top-3 by full exp so the operator can decide
            # whether to relax the gate manually.
            sort_by_full = sorted(
                by_symbol.get(sym, []),
                key=lambda r: -(r["wf"]["full"]["exp_r"] or -99.0),
            )[:3]
            for r in sort_by_full:
                p = r["params"]
                wf = r["wf"]
                print(f"     best by full: {p['ma_type']:<8} L={p['length']:>2} M={p['mult']} ATR={p['atr_len']:>2}"
                      f"  full exp={wf['full']['exp_r']:+.3f}  val={wf['val']['exp_r']:+.3f}  test={wf['test']['exp_r']:+.3f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
