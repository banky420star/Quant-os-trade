"""Grid probe — test one strategy across symbols × condition variants (solo, no live).

Each symbol is evaluated individually. Variants sweep RR, session tightness,
and entry strictness. Subagents run one strategy each in parallel.

Run:
  python scripts/strategy_probe_grid.py --strategy session_vol_breakout
  python scripts/strategy_probe_grid.py --all-strategies
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.strategy_probe_solo import (  # noqa: E402
    COST_R,
    FOLDS,
    MIN_EXP_R,
    MIN_TRADES,
    MIN_WR,
    STRATEGIES,
    WARMUP,
    fold_positive,
    load_m5,
    prep_features,
    simulate,
    _bars_sides,
)

ALL_SYMBOLS = [
    "XAUUSDm", "USOILm", "BTCUSDm", "EURUSDm", "GBPUSDm", "USDJPYm",
    "USDCHFm", "AUDUSDm", "US500m", "US30m", "NAS100m", "UK100m", "FR40m",
]

TIGHT_SESSIONS = frozenset({"london_open", "overlap_london_ny"})
WIDE_PRIME = frozenset({"london_open", "overlap_london_ny", "london_mid", "new_york", "asia"})


@dataclass(frozen=True)
class Variant:
    name: str
    rr: float = 1.5
    sl_atr: float = 1.2
    sessions: frozenset[str] | None = None  # None = use strategy default
    strict: bool = False


VARIANTS: list[Variant] = [
    Variant("base", rr=1.5, sl_atr=1.2),
    Variant("rr2", rr=2.0, sl_atr=1.2),
    Variant("rr2_sl1", rr=2.0, sl_atr=1.0),
    Variant("tight_session", rr=1.5, sl_atr=1.2, sessions=TIGHT_SESSIONS),
    Variant("rr2_tight", rr=2.0, sl_atr=1.0, sessions=TIGHT_SESSIONS),
    Variant("strict_rr2", rr=2.0, sl_atr=1.0, sessions=TIGHT_SESSIONS, strict=True),
]


def _session_mask(df: pd.DataFrame, sessions: frozenset[str] | None) -> pd.Series:
    if sessions is None:
        return df["prime_session"] if "prime_session" in df.columns else pd.Series(True, index=df.index)
    return df["session"].isin(list(sessions))


def detect_variant(
    strategy: str,
    df: pd.DataFrame,
    variant: Variant,
) -> tuple[np.ndarray, np.ndarray]:
    """Parameterized detectors per strategy + variant."""
    sess = _session_mask(df, variant.sessions)
    strict = variant.strict

    if strategy == "session_vol_breakout":
        atr_thr = 0.65 if strict else 0.55
        vol_thr = 1.25 if strict else 1.15
        m = sess & (df["atr_pct"] > atr_thr) & (df["vol_ratio"] > vol_thr)
        return _bars_sides(m, df["breakout"], df["breakdown"])

    if strategy == "low_vol_compression_fade":
        atr_thr = 0.25 if strict else 0.30
        bb_lo = 0.05 if strict else 0.08
        bb_hi = 0.95 if strict else 0.92
        m = df["atr_pct"] < atr_thr
        return _bars_sides(m, df["bb_position"] <= bb_lo, df["bb_position"] >= bb_hi)

    if strategy == "session_momentum_continuation":
        m = sess & (df["atr"] > df["atr_ma"] * (1.05 if strict else 1.0))
        buy = m & df["m5_bull"] & df["m15_bull"] & (df["ema20_slope"] > 0)
        sell = m & df["m5_bear"] & df["m15_bear"] & (df["ema20_slope"] < 0)
        return _bars_sides(m, buy, sell)

    if strategy == "opening_range_breakout":
        h_start, h_end = (8, 10) if not strict else (8, 9)
        m = (df["hour"] >= h_start) & (df["hour"] < h_end)
        prev_close = df["close"].shift(1)
        buy = m & (prev_close <= df["orb_high"]) & (df["close"] > df["orb_high"])
        sell = m & (prev_close >= df["orb_low"]) & (df["close"] < df["orb_low"])
        return _bars_sides(m, buy, sell)

    if strategy == "vol_spike_exhaustion_fade":
        mult = 2.5 if strict else 2.0
        retrace_thr = 0.6 if strict else 0.5
        spike = (df["bar_range"] > mult * df["atr"]).shift(1)
        sp_close = df["close"].shift(1)
        sp_open = df["open"].shift(1)
        sp_range = df["bar_range"].shift(1).replace(0, np.nan)
        retrace = (df["close"] - sp_close).abs() / sp_range
        m = spike & (retrace >= retrace_thr)
        buy = m & (sp_close < sp_open)
        sell = m & (sp_close > sp_open)
        return _bars_sides(m, buy, sell)

    if strategy == "session_stoch_pullback":
        k_lo = 35 if strict else 40
        k_hi = 65 if strict else 60
        m = sess
        prev_k = df["stoch_k"].shift(1)
        buy = m & df["m5_bull"] & df["m15_bull"] & (prev_k < k_lo) & df["stoch_bull_cross"]
        sell = m & df["m5_bear"] & df["m15_bear"] & (prev_k > k_hi) & df["stoch_bear_cross"]
        return _bars_sides(m, buy, sell)

    raise KeyError(strategy)


def evaluate_cell(
    symbol: str,
    strategy: str,
    variant: Variant,
    df: pd.DataFrame,
) -> dict[str, Any]:
    bars, sides = detect_variant(strategy, df, variant)
    keep = bars >= WARMUP
    bars = bars[keep]
    sides = sides[keep]
    rs = simulate(df, bars, sides, rr=variant.rr, sl_atr=variant.sl_atr)
    n = len(rs)
    if n == 0:
        return {
            "symbol": symbol,
            "strategy": strategy,
            "variant": variant.name,
            "rr": variant.rr,
            "sl_atr": variant.sl_atr,
            "sessions": sorted(variant.sessions) if variant.sessions else "prime",
            "strict": variant.strict,
            "n_trades": 0,
            "verdict": "NO_TRADES",
            "pass": False,
            "score": -999.0,
        }
    wins = sum(1 for r in rs if r > 0)
    wr = wins / n * 100
    exp_r = float(np.mean(rs))
    pos, total = fold_positive(rs)
    passed = n >= MIN_TRADES and exp_r >= MIN_EXP_R and wr >= MIN_WR and pos >= 2
    # Ranking score: expectancy weighted by sample + fold bonus
    score = exp_r * min(1.0, n / MIN_TRADES) + (0.05 * pos)
    return {
        "symbol": symbol,
        "strategy": strategy,
        "variant": variant.name,
        "rr": variant.rr,
        "sl_atr": variant.sl_atr,
        "sessions": sorted(variant.sessions) if variant.sessions else "prime",
        "strict": variant.strict,
        "n_trades": n,
        "win_rate_pct": round(wr, 1),
        "expectancy_r": round(exp_r, 3),
        "total_r": round(float(np.sum(rs)), 2),
        "fold_positive": f"{pos}/{total}",
        "pass": passed,
        "verdict": "PASS" if passed else "FAIL",
        "score": round(score, 4),
        "description": STRATEGIES[strategy][0],
    }


def run_strategy_grid(strategy: str, symbols: list[str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        try:
            df = prep_features(load_m5(sym))
        except FileNotFoundError:
            continue
        for variant in VARIANTS:
            rows.append(evaluate_cell(sym, strategy, variant, df))

    passed = [r for r in rows if r.get("pass")]
    ranked = sorted(rows, key=lambda x: (x.get("pass", False), x.get("score", -999)), reverse=True)
    by_symbol: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        sym_rows = [r for r in rows if r["symbol"] == sym]
        if sym_rows:
            best = max(sym_rows, key=lambda x: (x.get("pass", False), x.get("score", -999)))
            by_symbol[sym] = best

    return {
        "strategy": strategy,
        "description": STRATEGIES[strategy][0],
        "cost_r": COST_R,
        "gate": {
            "min_trades": MIN_TRADES,
            "min_expectancy_r": MIN_EXP_R,
            "min_win_rate_pct": MIN_WR,
            "min_fold_positive": "2/3",
        },
        "variants_tested": [v.name for v in VARIANTS],
        "symbols_tested": symbols,
        "total_cells": len(rows),
        "passed_cells": len(passed),
        "best_overall": ranked[0] if ranked else None,
        "top10": ranked[:10],
        "by_symbol_best": by_symbol,
        "all_results": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=list(STRATEGIES.keys()))
    ap.add_argument("--all-strategies", action="store_true")
    ap.add_argument("--symbols", nargs="+", default=ALL_SYMBOLS)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if not args.strategy and not args.all_strategies:
        ap.error("Provide --strategy NAME or --all-strategies")

    names = list(STRATEGIES.keys()) if args.all_strategies else [args.strategy]
    combined: list[dict[str, Any]] = []
    for name in names:
        payload = run_strategy_grid(name, args.symbols)
        combined.append(payload)
        out = args.out or str(ROOT / "state" / "probe_grid" / f"{name}.json")
        path = Path(out) if len(names) == 1 else Path(str(ROOT / "state" / "probe_grid" / f"{name}.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        best = payload.get("best_overall") or {}
        print(
            f"{name}: passed={payload['passed_cells']}/{payload['total_cells']}  "
            f"best={best.get('symbol','?')} {best.get('variant','?')} "
            f"exp={best.get('expectancy_r',0):+.3f}R folds={best.get('fold_positive','?')} "
            f"{'PASS' if best.get('pass') else 'FAIL'}"
        )

    if len(names) > 1:
        master = ROOT / "state" / "probe_grid" / "master_summary.json"
        master.write_text(json.dumps({"strategies": combined}, indent=2), encoding="utf-8")
        print(f"Wrote {master}")


if __name__ == "__main__":
    main()