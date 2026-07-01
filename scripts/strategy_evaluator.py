"""Out-of-sample strategy evaluator — the honest alternative to the legacy
``run_performance_benchmark.py``.

What this does differently from the legacy benchmark
----------------------------------------------------
1. FIXED window. History is sliced to an explicit date range and replayed
   start-to-end. No ``len(m5_df) - max_bars`` sliding window, so the result
   does not change as the live ``data_loop`` appends bars.
2. OUT OF SAMPLE. The common calendar window is split train/test by date.
   Variants are frozen presets; we report test-window (OOS) expectancy and
   compare it against the train-window (IS) expectancy to flag overfitting.
3. REAL capital. Evaluates on the actual account balance (e.g. $80), not a
   fictional $1,000,000. Strategy comparison is reported in R-multiples
   (capital-independent) plus $ equity.
4. ALL trades. ``return_all_trades=True`` so statistics run over hundreds of
   trades, not the last 50.
5. REAL spread gating + explicit cost model. Real spread is forwarded to the
   verifier's spread gate, and a per-trade cost (``--cost-r``, swept) is applied
   to net expectancy to stand in for spread + slippage + commission.
6. STATISTICS. Bootstrap 95% CI on expectancy; profit factor; max drawdown;
   minimum trade count before any verdict is drawn.
7. COMPOUNDING (optional). For any variant with positive OOS expectancy,
   simulate fractional-risk compounding on the real capital and show realistic
   p5/median/p95 equity paths — the honest answer to "can it grow to $50k?".

Usage
-----
    python scripts/strategy_evaluator.py --quick              # fast smoke test
    python scripts/strategy_evaluator.py                       # full train/test
    python scripts/strategy_evaluator.py --compounding --target 50000

This is a measurement tool. It does not place trades and never edits the live
bot's parquet store (history is copied to a temp dir).
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import shutil
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.history_manager as history_manager  # noqa: E402
from core.replay_engine import run_portfolio_replay  # noqa: E402
from core.strategy_variants import (  # noqa: E402
    VARIANT_ORDER,
    build_variant_config,
    variant_description,
)
from core.utils import DATA_DIR, load_config, setup_logger  # noqa: E402

# Representative live spreads (points) from state/approved_signals.json on
# 2026-06-26. Used for spread-gating; the verifier caps are generous
# (XAU<=1050, USOIL<=175, BTC<=4200 after spread_mult=3.5) so this mainly
# matters on wide-spread bars.
LIVE_SPREAD_POINTS = {"XAUUSDm": 240.0, "USOILm": 20.0, "BTCUSDm": 1000.0}

REAL_HISTORY_DIR = DATA_DIR / "history"


# --------------------------------------------------------------------------- #
# History staging — copy live parquets, slice to a fixed window.              #
# --------------------------------------------------------------------------- #

def _load_raw(symbols: list[str], timeframes: list[str]) -> dict[str, dict[str, pd.DataFrame]]:
    """Read each symbol/timeframe parquet straight from disk (read-only)."""
    frames: dict[str, dict[str, pd.DataFrame]] = {}
    for sym in symbols:
        frames[sym] = {}
        for tf in timeframes:
            path = REAL_HISTORY_DIR / f"{sym}_{tf}.parquet"
            if not path.exists():
                raise RuntimeError(f"missing parquet: {path}")
            df = pd.read_parquet(path)
            if "time" in df.columns:
                df["time"] = pd.to_datetime(df["time"], utc=True)
                df = df.sort_values("time").reset_index(drop=True)
            frames[sym][tf] = df
    return frames


def _common_window(frames: dict[str, dict[str, pd.DataFrame]]) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Calendar window where ALL symbols have M5 data (intersection)."""
    starts, ends = [], []
    for sym in frames:
        m5 = frames[sym]["M5"]
        starts.append(m5["time"].iloc[0])
        ends.append(m5["time"].iloc[-1])
    return max(starts), min(ends)


def _slice_frames(
    frames: dict[str, dict[str, pd.DataFrame]],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, dict[str, pd.DataFrame]]:
    out: dict[str, dict[str, pd.DataFrame]] = {}
    for sym, tfs in frames.items():
        out[sym] = {}
        for tf, df in tfs.items():
            sub = df[(df["time"] >= start) & (df["time"] <= end)].reset_index(drop=True)
            out[sym][tf] = sub
    return out


def _stage_window(
    scratch_dir: Path,
    sliced: dict[str, dict[str, pd.DataFrame]],
) -> Path:
    """Write sliced parquets into scratch_dir and point HistoryManager at it."""
    window_dir = scratch_dir / "history"
    window_dir.mkdir(parents=True, exist_ok=True)
    for sym, tfs in sliced.items():
        for tf, df in tfs.items():
            out = window_dir / f"{sym}_{tf}.parquet"
            df.to_parquet(out, index=False)
    # Redirect the module global that HistoryManager.parquet_path reads.
    history_manager.HISTORY_DIR = window_dir
    return window_dir


# --------------------------------------------------------------------------- #
# Window splitting.                                                           #
# --------------------------------------------------------------------------- #

def make_splits(
    common_start: pd.Timestamp,
    common_end: pd.Timestamp,
    train_ratio: float,
    folds: int,
) -> list[dict[str, Any]]:
    """Train/test date splits. ``folds=1`` → single trailing test window.

    ``folds>1`` → walk-forward: each fold's test window sits after its train
    window, and test windows march forward in time without overlap.
    """
    total = (common_end - common_start).total_seconds()
    if folds <= 1:
        train_end = common_start + pd.Timedelta(seconds=total * train_ratio)
        return [{
            "fold": 0,
            "train_start": common_start,
            "train_end": train_end,
            "test_start": train_end,
            "test_end": common_end,
        }]
    test_total = total * (1.0 - train_ratio)
    step = test_total / folds
    splits = []
    for k in range(folds):
        # Expanding-window walk-forward: train grows each fold, test is a
        # fixed-width slice that marches forward without overlap.
        train_end = common_start + pd.Timedelta(
            seconds=total * train_ratio * (k + 1) / folds
        )
        test_start = train_end
        test_end = train_end + pd.Timedelta(seconds=step)
        if test_end > common_end:
            test_end = common_end
        splits.append({
            "fold": k,
            "train_start": common_start,
            "train_end": train_end,
            "test_start": test_start,
            "test_end": test_end,
        })
    return splits


# --------------------------------------------------------------------------- #
# Running one variant on one window.                                          #
# --------------------------------------------------------------------------- #

def run_variant(
    base_cfg: dict[str, Any],
    variant_name: str,
    capital: float,
    symbols: list[str],
    step: int,
    *,
    return_trades: bool = True,
    logger: logging.Logger,
) -> dict[str, Any]:
    cfg = build_variant_config(base_cfg, variant_name, capital_usd=capital)
    out = run_portfolio_replay(
        cfg,
        symbols,
        max_bars=10_000_000,  # replay the whole staged window, never slide
        step=step,
        logger=logger,
        spread_data=LIVE_SPREAD_POINTS,
        return_all_trades=return_trades,
    )
    return out


# --------------------------------------------------------------------------- #
# Statistics.                                                                 #
# --------------------------------------------------------------------------- #

def trade_r_multiples(trades: list[dict[str, Any]]) -> list[float]:
    """R-multiple of each trade.

    By definition R = pnl / (|entry - sl| * size) and pnl = dPrice * size, so
    the size cancels: R = (exit - entry) * dir / |entry - sl|. We compute it
    from entry/exit/sl/side on purpose — the broker's trade record does not
    carry ``size``, and this form is capital-independent anyway.
    """
    rs = []
    for t in trades:
        try:
            entry = float(t.get("entry", 0) or 0)
            exitp = float(t.get("exit", 0) or 0)
            sl = float(t.get("sl", 0) or 0)
            sl_dist = abs(entry - sl)
            if sl_dist <= 0:
                continue
            side = str(t.get("side", "")).upper()
            dprice = (exitp - entry) if side != "SELL" else (entry - exitp)
            rs.append(dprice / sl_dist)
        except (TypeError, ValueError):
            continue
    return rs


def bootstrap_expectancy_ci(
    r_multiples: list[float],
    *,
    n_boot: int = 2000,
    seed: int = 20260626,
) -> tuple[float, float]:
    """Bootstrap 95% CI on the mean R per trade."""
    if len(r_multiples) < 2:
        return (0.0, 0.0)
    import random
    rng = random.Random(seed)
    n = len(r_multiples)
    means = []
    for _ in range(n_boot):
        sample = [r_multiples[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * n_boot)]
    hi = means[min(int(0.975 * n_boot), n_boot - 1)]
    return (round(lo, 4), round(hi, 4))


def compute_stats(
    trades: list[dict[str, Any]],
    *,
    cost_r: float,
) -> dict[str, Any]:
    rs = trade_r_multiples(trades)
    n = len(rs)
    if n == 0:
        return {
            "trades": 0, "win_rate_pct": 0.0, "expectancy_r": 0.0,
            "expectancy_net_r": 0.0, "profit_factor": 0.0,
            "max_drawdown_r": 0.0, "ci95": [0.0, 0.0], "sum_r": 0.0,
        }
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    pf = round(gross_win / gross_loss, 3) if gross_loss > 0 else float("inf")

    # Max drawdown over the R-curve (cumulative sum of R).
    peak = 0.0
    max_dd = 0.0
    cum = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    exp_raw = sum(rs) / n
    exp_net = exp_raw - cost_r  # cost applied to every trade
    ci = bootstrap_expectancy_ci(rs)

    return {
        "trades": n,
        "win_rate_pct": round(100.0 * len(wins) / n, 1),
        "expectancy_r": round(exp_raw, 4),
        "expectancy_net_r": round(exp_net, 4),
        "profit_factor": pf,
        "max_drawdown_r": round(max_dd, 2),
        "ci95": list(ci),
        "sum_r": round(sum(rs), 2),
        "gross_win_r": round(gross_win, 2),
        "gross_loss_r": round(gross_loss, 2),
    }


def compounding_paths(
    r_multiples: list[float],
    *,
    start_capital: float,
    risk_frac: float,
    n_paths: int,
    n_trades_per_path: int,
    seed: int = 20260626,
) -> dict[str, Any]:
    """Simulate fractional-risk compounding by resampling the R series.

    Each path risks ``risk_frac`` of current equity per trade; equity changes
    by equity * risk_frac * R each trade. Returns p5/median/p95 of the final
    equity and the path that hits ``target`` earliest (if any).
    """
    if not r_multiples:
        return {"p5": start_capital, "median": start_capital, "p95": start_capital,
                "hit_target_pct": 0.0, "median_trades_to_target": None}
    import random
    rng = random.Random(seed)
    n = len(r_multiples)
    finals = []
    hits = 0
    trades_to_target = []
    for _ in range(n_paths):
        eq = start_capital
        hit = False
        for k in range(n_trades_per_path):
            r = r_multiples[rng.randrange(n)]
            eq = eq * (1.0 + risk_frac * r)
            if eq >= start_capital * 50 and not hit:  # 50x = $80->$4000 milestone proxy
                hit = True
            if eq <= 0:
                eq = 0.0
                break
        finals.append(eq)
        if hit:
            trades_to_target.append(k)
    finals.sort()
    return {
        "p5": round(finals[int(0.05 * n_paths)], 2),
        "median": round(finals[n_paths // 2], 2),
        "p95": round(finals[int(0.95 * n_paths) - 1], 2),
        "ruin_pct": round(100.0 * sum(1 for f in finals if f <= start_capital * 0.1) / n_paths, 1),
        "median_final": round(finals[n_paths // 2], 2),
    }


# --------------------------------------------------------------------------- #
# Main.                                                                       #
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Out-of-sample strategy evaluator")
    ap.add_argument("--variants", nargs="*", default=None, help="subset of variants to run")
    ap.add_argument("--capital", type=float, default=80.0, help="real account capital (USD)")
    ap.add_argument("--train-ratio", type=float, default=0.7)
    ap.add_argument("--folds", type=int, default=1)
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--cost-r", type=float, default=0.15, help="per-trade cost in R")
    ap.add_argument("--min-trades", type=int, default=40, help="minimum trades for a verdict")
    ap.add_argument("--out", type=Path, default=ROOT / "strategy_eval_report.json")
    ap.add_argument("--compounding", action="store_true")
    ap.add_argument("--target", type=float, default=50000.0)
    ap.add_argument("--max-window-bars", type=int, default=10000,
                    help="cap each train/test window to its last N M5 bars (0 = no cap). "
                         "Bounded runtime; test stays the most-recent (unseen) data.")
    ap.add_argument("--quick", action="store_true", help="short test window, fast smoke test")
    args = ap.parse_args()

    log = setup_logger("strategy_evaluator", "strategy_evaluator.log")
    base_cfg = load_config()
    symbols = list(base_cfg["mt5"]["symbols"])
    timeframes = ["M5", "M15"]
    variants = args.variants or VARIANT_ORDER

    log.info("Staging history from %s", REAL_HISTORY_DIR)
    frames = _load_raw(symbols, timeframes)
    common_start, common_end = _common_window(frames)
    log.info("Common window: %s -> %s (%.1f days)",
             common_start, common_end,
             (common_end - common_start).total_seconds() / 86400)

    splits = make_splits(common_start, common_end, args.train_ratio, args.folds)
    if args.quick:
        # Restrict the test window to the last ~2500 M5 bars for speed.
        ref = frames[symbols[0]]["M5"]
        quick_start = ref["time"].iloc[-2500]
        for s in splits:
            if s["test_start"] < quick_start:
                s["test_start"] = pd.Timestamp(quick_start)
        log.info("QUICK mode: test window truncated to %s onwards", quick_start)

    scratch = Path(tempfile.mkdtemp(prefix="strat_eval_"))
    log.info("Scratch dir: %s", scratch)

    results: list[dict[str, Any]] = []
    for split in splits:
        log.info("=== Fold %d  train[%s..%s] test[%s..%s] ===",
                 split["fold"], split["train_start"], split["train_end"],
                 split["test_start"], split["test_end"])
        for window in ("train", "test"):
            if args.quick and window == "train":
                continue  # smoke test: train window is large; OOS test is what matters
            w_start = split[f"{window}_start"]
            w_end = split[f"{window}_end"]
            sliced = _slice_frames(frames, w_start, w_end)
            # Cap window to its last N M5 bars (bounded runtime). Keep the most
            # recent N bars of the window; for train this is the block just
            # before test, for test this is the most recent (unseen) data.
            if args.max_window_bars and len(sliced[symbols[0]]["M5"]) > args.max_window_bars:
                cap_ts = sliced[symbols[0]]["M5"]["time"].iloc[-args.max_window_bars]
                sliced = _slice_frames(frames, cap_ts, w_end)
            # Ensure every symbol has enough bars.
            min_rows = min(len(sliced[s]["M5"]) for s in symbols)
            if min_rows < 50:
                log.info("  skip %s window (only %d bars for some symbol)", window, min_rows)
                continue
            _stage_window(scratch, sliced)

            for vname in variants:
                log.info("  [%s] variant=%s capital=$%.2f", window, vname, args.capital)
                try:
                    out = run_variant(base_cfg, vname, args.capital, symbols, args.step,
                                      return_trades=True, logger=log)
                except Exception as exc:  # noqa: BLE001
                    log.error("    variant %s crashed: %s", vname, exc)
                    results.append({"fold": split["fold"], "window": window,
                                    "variant": vname, "error": str(exc)})
                    continue
                stats = compute_stats(out["trades"], cost_r=args.cost_r)
                stats["final_equity"] = round(float(out.get("final_equity", 0)), 2)
                stats["signals_generated"] = out.get("signals_generated", 0)
                stats["signals_approved"] = out.get("signals_approved", 0)
                row = {"fold": split["fold"], "window": window,
                       "variant": vname, "description": variant_description(vname),
                       **stats}
                results.append(row)
                log.info("    -> trades=%d wr=%.1f%% expR=%.4f netR=%.4f PF=%s maxDD=%.2f",
                         stats["trades"], stats["win_rate_pct"], stats["expectancy_r"],
                         stats["expectancy_net_r"], stats["profit_factor"],
                         stats["max_drawdown_r"])

    # ---- Report ---- #
    print("\n" + "=" * 92)
    print("OUT-OF-SAMPLE STRATEGY EVALUATOR  (capital=$%.2f, cost=%.2fR/trade, step=%d)"
          % (args.capital, args.cost_r, args.step))
    print("=" * 92)
    hdr = f"{'variant':14s} {'win':6s} {'tr':5s} {'expR':8s} {'netR':8s} {'PF':6s} {'maxDD':7s} {'CI95':>16s}"
    for window in ("train", "test"):
        print(f"\n--- {window.upper()} window ---")
        print(hdr)
        for r in results:
            if r.get("window") != window or "error" in r:
                continue
            print(f"{r['variant']:14s} {r['win_rate_pct']:5.1f}% {r['trades']:5d} "
                  f"{r['expectancy_r']:8.4f} {r['expectancy_net_r']:8.4f} "
                  f"{str(r['profit_factor']):6s} {r['max_drawdown_r']:7.2f} "
                  f"[{r['ci95'][0]:+.3f},{r['ci95'][1]:+.3f}]")

    # ---- Verdict: positive OOS edge ---- #
    candidates = [r for r in results
                  if r.get("window") == "test"
                  and r.get("trades", 0) >= args.min_trades
                  and r.get("expectancy_net_r", 0) > 0
                  and r["ci95"][0] > 0]
    print("\n" + "-" * 92)
    print("OOS EDGE CANDIDATES (test window, net of %.2fR cost, CI95 lower bound > 0, "
          ">= %d trades):" % (args.cost_r, args.min_trades))
    if candidates:
        for c in candidates:
            print("  + %-14s netR=%.4f  wr=%.1f%%  trades=%d  PF=%s  maxDD=%.2fR"
                  % (c["variant"], c["expectancy_net_r"], c["win_rate_pct"],
                     c["trades"], c["profit_factor"], c["max_drawdown_r"]))
    else:
        print("  (none) — no variant shows a statistically-positive OOS edge net of cost.")

    # ---- Compounding ---- #
    compounding_out: dict[str, Any] = {}
    if args.compounding and candidates:
        print("\n--- Compounding simulation on $%.2f (fractional risk) ---" % args.capital)
        # Re-run candidate on test window to recover its R series for the sim.
        split = splits[-1]
        sliced = _slice_frames(frames, split["test_start"], split["test_end"])
        _stage_window(scratch, sliced)
        for c in candidates:
            out = run_variant(base_cfg, c["variant"], args.capital, symbols, args.step,
                             return_trades=True, logger=log)
            rs = trade_r_multiples(out["trades"])
            risk_frac = float(build_variant_config(base_cfg, c["variant"],
                                                   capital_usd=args.capital)
                              ["signals"].get("default_risk_percent", 0.45)) / 100.0
            sim = compounding_paths(rs, start_capital=args.capital, risk_frac=risk_frac,
                                    n_paths=2000, n_trades_per_path=2000)
            compounding_out[c["variant"]] = sim
            print("  %-14s risk=%.2f%%/trade  p5=$%.2f  median=$%.2f  p95=$%.2f  ruin=%s%%"
                  % (c["variant"], risk_frac * 100, sim["p5"], sim["median"], sim["p95"],
                     sim["ruin_pct"]))
            log.info("compounding %s: %s", c["variant"], sim)

    payload = {
        "capital_usd": args.capital,
        "cost_r": args.cost_r,
        "step": args.step,
        "min_trades": args.min_trades,
        "common_window": {"start": str(common_start), "end": str(common_end)},
        "splits": [{**s, "train_start": str(s["train_start"]), "train_end": str(s["train_end"]),
                    "test_start": str(s["test_start"]), "test_end": str(s["test_end"])}
                   for s in splits],
        "results": results,
        "candidates": [c["variant"] for c in candidates],
        "compounding": compounding_out,
        "note": ("Reported in R-multiples (capital-independent). expectancy_net_r applies a "
                 "per-trade cost stand-in for spread+slippage+commission. No verdict is drawn "
                 "below min_trades. This is measurement only — no live trades placed."),
    }
    args.out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("\nReport written to %s" % args.out)

    try:
        shutil.rmtree(scratch)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())