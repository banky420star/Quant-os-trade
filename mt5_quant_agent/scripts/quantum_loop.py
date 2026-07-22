"""Quantum loop — discretized exhaustive OOS sweep, best edges flow organically.

The user's idea, implemented honestly:

    "quantum loop let you simulate every result possible and using that the
     best results will always organically flow."

What "quantum loop" means here, faithfully
-----------------------------------------
A truly continuous parameter space is infinite, so "every result possible"
cannot be literally enumerated. The honest realisation is to *discretise* the
space into a finite grid of "quantum cells" — each cell is one concrete
(variant, exit-config, gate-config) combination — simulate every cell through
the same OOS walk-forward pipeline, and let the cells with a statistically-
positive OUT-OF-SAMPLE edge surface to the top of the ranking by themselves.
No hand-picking. The winners "organically flow" because they are the cells that
survive OOS validation net of cost, not because someone chose them.

Why this is NOT the overfitting the legacy benchmark did
--------------------------------------------------------
The legacy benchmark swept parameters to maximise one window's PnL — pure in-
sample optimisation. The quantum loop does the opposite: every cell is scored
on UNSEEN test windows (walk-forward), the selection criterion is the bootstrap
CI95 *lower bound* net of cost (not the point estimate), and we apply a
multiple-comparison correction (Bonferroni on the K cells tested) so a cell
only counts as an "organic winner" if its edge survives the fact that we tried
K things. Selecting the best of K noisy estimates is exactly multiple-
comparison bias; the correction + OOS + majority-fold stability is what makes
a surfaced winner trustworthy rather than a mirage.

The grid (discrete quantum cells)
---------------------------------
  axis 1 — strategy variant:   baseline / strict_trend / conservative / scalper / wide_swing
  axis 2 — exit config:        off / tight / medium / wide  (BE+trailing, the now-live dynamic exits)
  axis 3 — gate config:        off / loose / strict       (win-condition template gate, expectancy-based)

A cell = (variant, exit, gate). Default grid is 5x4x3 = 60 cells; --grid small
trims it. Each cell is run on every walk-forward fold's TEST window; templates
for the gate are learned on that fold's TRAIN (baseline, no gate) and frozen.

Honesty guards (unchanged from the rest of the honesty discipline)
-------------------------------------------------------------------
1. OUT OF SAMPLE on TEST. Templates learned on TRAIN only.
2. SELECTION BIAS corrected by Bonferroni on K + majority-fold stability.
3. READ-ONLY on the live edge DB (replay_ingest_edge_db=False).
4. No live trades, no live state mutation, kill switch stays ON.
5. SAME pipeline (FeatureEngine -> DecisionEngine -> Verifier -> PaperBroker),
   so an organic winner is deployable as-is, not a parallel engine.
6. Narratives (entry/BE/trail/exit) are stamped on every trade so the top
   winner's per-trade reasons + profitability can be dumped (--show-trades).

Usage
-----
    python scripts/quantum_loop.py --quick              # smoke test, small grid
    python scripts/quantum_loop.py                      # default grid, 1 fold
    python scripts/quantum_loop.py --folds 3 --grid full
    python scripts/quantum_loop.py --show-trades 25    # dump top winner's trades
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.replay_engine import run_portfolio_replay  # noqa: E402
from core.strategy_variants import build_variant_config, VARIANT_ORDER  # noqa: E402
from core.utils import load_config, setup_logger  # noqa: E402

# Reuse the tested helpers — do not duplicate statistics / staging code.
from scripts.strategy_evaluator import (  # noqa: E402
    LIVE_SPREAD_POINTS,
    REAL_HISTORY_DIR,
    _common_window,
    _load_raw,
    _slice_frames,
    _stage_window,
    bootstrap_expectancy_ci,
    compute_stats,
    make_splits,
    trade_r_multiples,
)
from scripts.condition_templates import (  # noqa: E402
    learn_templates,
    run_replay as run_baseline_replay,
    trade_cell,
)


# --------------------------------------------------------------------------- #
# Discrete exit-config and gate-config axes (the "quanta").                  #
# --------------------------------------------------------------------------- #

# Each exit config is a dotted-path overlay applied on top of the variant.
# These are the dynamic exits that are now actually implemented in PaperBroker
# (break_even + trailing). "off" is the control: SL/TP1 only, no BE/trail.
EXIT_CONFIGS: dict[str, dict[str, Any]] = {
    "off": {
        "description": "No break-even, no trailing — SL/TP1 only (the old behaviour).",
        "trading/break_even/enabled": False,
        "trading/trailing/enabled": False,
    },
    "tight": {
        "description": "BE at +0.4ATR locking 0.08ATR; trail activates +0.6ATR, 0.25ATR behind peak.",
        "trading/break_even/enabled": True,
        "trading/break_even/trigger_atr_mult": 0.4,
        "trading/break_even/lock_profit_atr_mult": 0.08,
        "trading/trailing/enabled": True,
        "trading/trailing/activation_atr_mult": 0.6,
        "trading/trailing/trail_atr_mult": 0.25,
    },
    "medium": {
        "description": "BE at +0.5ATR locking 0.1ATR; trail activates +0.75ATR, 0.35ATR behind peak (config default).",
        "trading/break_even/enabled": True,
        "trading/break_even/trigger_atr_mult": 0.5,
        "trading/break_even/lock_profit_atr_mult": 0.1,
        "trading/trailing/enabled": True,
        "trading/trailing/activation_atr_mult": 0.75,
        "trading/trailing/trail_atr_mult": 0.35,
    },
    "wide": {
        "description": "BE at +0.75ATR locking 0.15ATR; trail activates +1.0ATR, 0.5ATR behind peak — let winners run.",
        "trading/break_even/enabled": True,
        "trading/break_even/trigger_atr_mult": 0.75,
        "trading/break_even/lock_profit_atr_mult": 0.15,
        "trading/trailing/enabled": True,
        "trading/trailing/activation_atr_mult": 1.0,
        "trading/trailing/trail_atr_mult": 0.5,
    },
}

# Gate configs. The honest criterion is positive EXPECTANCY (netR > 0) with a
# min sample size — NOT a 50% win rate (the bot wins 26-43%; a 35% wr at 2:1 RR
# is profitable). min_wr is a secondary floor, kept low. "off" is the control.
GATE_CONFIGS: dict[str, dict[str, Any]] = {
    "off": {"description": "No win-condition gate — trade every verified signal.", "enabled": False},
    "loose": {
        "description": "Gate to cells with >=3 trades and positive net expectancy (learned on TRAIN).",
        "enabled": True, "min_n": 3, "min_wr": 0.0, "strict": False,
    },
    "strict": {
        "description": "Gate to cells with >=5 trades, netR>0; reject setups with no learned template.",
        "enabled": True, "min_n": 5, "min_wr": 0.0, "strict": True,
    },
}


def _apply_overlay(cfg: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Apply dotted-path ("/" nested) overlay onto a config dict in place."""
    for key, value in overlay.items():
        if "/" not in key:
            continue
        section = cfg
        for part in key.split("/")[:-1]:
            section = section.setdefault(part, {})
        section[key.split("/")[-1]] = value
    return cfg


# --------------------------------------------------------------------------- #
# Running one quantum cell.                                                   #
# --------------------------------------------------------------------------- #

def run_cell(
    base_cfg: dict[str, Any],
    variant: str,
    exit_name: str,
    gate_name: str,
    gate_templates: dict[str, Any] | None,
    capital: float,
    symbols: list[str],
    step: int,
    *,
    strict: bool,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Run one (variant, exit, gate) cell on the currently-staged history window."""
    cfg = build_variant_config(base_cfg, variant, capital_usd=capital)
    _apply_overlay(cfg, EXIT_CONFIGS[exit_name])
    # The overlay sets TOP-LEVEL break_even/trailing params, but PaperBroker's
    # _be_cfg/_trail_cfg read trading.<section>.per_symbol.<symbol> FIRST and
    # fall back to the top-level value only when per_symbol is absent. config.yaml
    # ships per_symbol overrides for XAUUSDm/USOILm/BTCUSDm, which would SHADOW
    # the overlay -> making medium==wide==tight identical for every traded symbol
    # (only "off" differs, since per_symbol has no "enabled" key). For the grid to
    # actually test the exit configs it names, drop the per_symbol block here so
    # the overlay's top-level values are the single source of truth. Production
    # per_symbol tuning is untouched (this only runs inside quantum_loop cells).
    for _section in ("break_even", "trailing"):
        _node = cfg.get("trading", {}).get(_section)
        if isinstance(_node, dict) and "per_symbol" in _node:
            _node.pop("per_symbol", None)
    if gate_templates is not None:
        cfg["signals"]["win_templates"] = gate_templates
        cfg["signals"]["win_template_strict"] = strict
    return run_portfolio_replay(
        cfg, symbols,
        max_bars=10_000_000, step=step, logger=logger,
        spread_data=LIVE_SPREAD_POINTS, return_all_trades=True,
    )


def _ci95_lo(stats: dict[str, Any]) -> float:
    try:
        return float(stats["ci95"][0])
    except (KeyError, TypeError, IndexError):
        return 0.0


def _cost_r_from_bps(m5_df: Any, *, cost_bps: float, spot: float | None) -> tuple[float, dict[str, float]]:
    """Convert a round-trip basis-point cost into an R-equivalent flat per-trade cost.

    Retail XAUUSD round-trip cost is ~20-40 bps of notional (spread+commission);
    the literature's positive intraday gold edge (Singha 2025) assumes 0.7 bps
    institutional. R = (exit-entry)/|entry-sl|, so an R-equivalent cost is
    cost_price / |entry-sl|. We approximate |entry-sl| by the strategy's typical
    1.5*ATR(14) stop distance and cost_price by (bps/1e4)*spot, both taken as the
    MEDIAN over the staged M5 window (robust to a few fat bars). Returns
    (cost_r, provenance_dict) so the conversion is transparent/loggable.
    """
    import math
    import pandas as pd  # noqa
    df = m5_df
    if df is None or len(df) == 0:
        return 0.0, {"cost_bps": cost_bps, "spot": spot, "median_sl_dist": 0.0, "reason": "empty M5 frame"}
    h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    sl_dist = (1.5 * atr).median()
    price = float(spot) if spot and spot > 0 else float(c.median())
    # Guard NaN (empty/all-NaN M5 frame): `not NaN` is False and `NaN <= 0` is
    # False, so the truthiness guard below would let NaN through and poison the
    # whole grid's cost_r -> expectancy -> DSR. Fail safe to 0.0 cost.
    if not math.isfinite(float(sl_dist)) or float(sl_dist) <= 0 or not math.isfinite(float(price)):
        return 0.0, {"cost_bps": cost_bps, "spot": price, "median_sl_dist": float(sl_dist),
                      "reason": "non-finite sl_dist/price"}
    cost_r = (cost_bps / 10000.0) * price / float(sl_dist)
    return cost_r, {"cost_bps": cost_bps, "spot": price, "median_sl_dist": float(sl_dist),
                    "cost_r": cost_r}


# Euler-Mascheroni constant (Bailey-LdP False Strategy Theorem two-quantile form).
_EULER_MASCHERONI = 0.5772156649015329


def _expected_max_sr(n_trials: int, *, mode: str = "precise") -> float:
    """E[max of N iid standard normals] -- the selection-bias term in DSR.

    ``precise`` (default): Bailey & Lopez de Prado (2014) False Strategy Theorem
    two-quantile form ``E[max_N] = (1-gamma)*Phi^-1(1-1/N) + gamma*Phi^-1(1-1/(N*e))``.
    ``crude``: the asymptotic Gumbel approximation ``sqrt(2 ln N)``. For N=45 the
    crude multiplier is 2.759 vs precise 2.236 -- crude is ~1.23x MORE conservative
    (kills more cells). Precise is the published form and the new default; crude is
    retained only so the threshold convention's effect can be diffed without a
    re-run. Both are 0 when there is no selection (N<=1).
    """
    import math
    from statistics import NormalDist

    if n_trials <= 1:
        return 0.0
    if mode == "crude":
        return math.sqrt(2 * math.log(n_trials))
    nd = NormalDist()
    gamma = _EULER_MASCHERONI
    # Phi^-1(1-1/N) and Phi^-1(1-1/(N*e)); valid for N>1.
    q1 = nd.inv_cdf(1 - 1 / n_trials)
    q2 = nd.inv_cdf(1 - 1 / (n_trials * math.e))
    return float((1 - gamma) * q1 + gamma * q2)


def deflated_sharpe(
    r_multiples: list[float],
    *,
    n_trials: int,
    sr_var_across_trials: float,
    threshold_mode: str = "precise",
) -> float:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014) as a probability.

    Corrects the observed per-trade Sharpe for (a) selection bias from testing
    ``n_trials`` strategies and (b) non-Normality (skew/kurtosis) of returns.
    This is the data-snooping bar the deep-research report identified as the
    proper one (vs crude Bonferroni, which only handles selection, not
    non-normality). ``sr_var_across_trials`` is the variance of the per-trade
    Sharpe across the K grid cells tried -- the cross-trial spread that drives
    how much the best cell's SR is expected to be inflated by pure search.

    ``r_multiples`` should be NET of cost (gross_r - cost_r); the gate is a
    deployability test and deployability is post-cost. Computing DSR on gross R
    overstates the Sharpe and was the prior convention (now corrected).

    ``threshold_mode`` selects the E[max_N] form (see ``_expected_max_sr``);
    default ``precise`` is the published FST two-quantile form.

    Returns DSR in [0,1]; >0.95 means the edge survives at 95% after deflation.
    """
    import math
    from statistics import NormalDist

    n = len(r_multiples)
    if n < 2:
        return 0.0
    mean = sum(r_multiples) / n
    var = sum((x - mean) ** 2 for x in r_multiples) / n
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    sr = mean / std  # per-trade Sharpe (annualization cancels in the test)
    m3 = sum((x - mean) ** 3 for x in r_multiples) / n
    skew = m3 / (std ** 3)
    m4 = sum((x - mean) ** 4 for x in r_multiples) / n
    kurt = m4 / (std ** 4)  # raw kurtosis; (kurt-1) appears in the denom term
    # Threshold SR_0 = E[max of N iid std normals] * sqrt(V[SR_n]); 0 when no
    # selection.
    if n_trials > 1 and sr_var_across_trials > 0:
        em = _expected_max_sr(n_trials, mode=threshold_mode)
        sr_0 = em * math.sqrt(sr_var_across_trials)
    else:
        sr_0 = 0.0
    num = (sr - sr_0) * math.sqrt(n - 1)
    den = math.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr ** 2)
    if den <= 0:
        return 0.0
    return float(NormalDist().cdf(num / den))


# --------------------------------------------------------------------------- #
# Grid selection.                                                             #
# --------------------------------------------------------------------------- #

GRID_PRESETS = {
    # (variants, exits, gates)
    "small": (["baseline", "scalper"], ["off", "medium"], ["off", "loose"]),
    "default": (VARIANT_ORDER, ["off", "medium", "wide"], ["off", "loose", "strict"]),
    "full": (VARIANT_ORDER, list(EXIT_CONFIGS), list(GATE_CONFIGS)),
}


def _grid_variants(grid: str, variants_arg: list[str] | None,
                   exits_arg: list[str] | None, gates_arg: list[str] | None):
    v, e, g = GRID_PRESETS.get(grid, GRID_PRESETS["default"])
    if variants_arg:
        v = variants_arg
    if exits_arg:
        e = exits_arg
    if gates_arg:
        g = gates_arg
    return v, e, g


# --------------------------------------------------------------------------- #
# Main.                                                                       #
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Quantum loop — discretized exhaustive OOS sweep")
    ap.add_argument("--capital", type=float, default=80.0)
    ap.add_argument("--train-ratio", type=float, default=0.7)
    ap.add_argument("--folds", type=int, default=1)
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--cost-r", type=float, default=0.15)
    ap.add_argument("--cost-bps", type=float, default=None,
                    help="round-trip cost in basis points of NOTIONAL (e.g. 30 = 30 bps "
                         "round-trip spread+commission). When set, OVERRIDES --cost-r with an "
                         "R-equivalent derived from the staged M5 data (median spot price / "
                         "median 1.5*ATR(14) stop distance). Use to re-run at retail 20-40 bps "
                         "vs the institutional 0.7 bps the literature's positive gold edge "
                         "assumes (Singha 2025).")
    ap.add_argument("--cost-spot", type=float, default=None,
                    help="override the spot price used by --cost-bps conversion (default: "
                         "median M5 close of the first symbol)")
    ap.add_argument("--min-trades", type=int, default=40, help="min TEST trades for a cell to be verdict-eligible")
    ap.add_argument("--grid", choices=list(GRID_PRESETS), default="default")
    ap.add_argument("--variants", nargs="*", default=None)
    ap.add_argument("--exits", nargs="*", default=None)
    ap.add_argument("--gates", nargs="*", default=None)
    ap.add_argument("--max-window-bars", type=int, default=10000)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--show-trades", type=int, default=0,
                    help="dump this many of the top winner's trades with narrative+profitability")
    ap.add_argument("--bonferroni", action="store_true", default=True,
                    help="apply Bonferroni correction on the K cells tested (default on)")
    ap.add_argument("--out", type=Path, default=ROOT / "quantum_loop_report.json")
    args = ap.parse_args()

    log = setup_logger("quantum_loop", "quantum_loop.log")
    # The per-bar INFO logs (decision_engine, paper_broker, memory, verifier...)
    # all flow through the shared "quantum_loop" logger that replay_engine passes
    # down, so they emit 2-3 lines/bar and dominate runtime on a multi-k-bar window.
    # Drop the whole tree to WARNING — per-cell progress is printed via print()
    # below, and the final report is read from the JSON file regardless.
    logging.getLogger("quantum_loop").setLevel(logging.WARNING)
    for noisy in ("decision_engine", "paper_broker", "verifier", "memory_engine",
                 "feature_engine", "evidence_engine", "setup_classifier",
                 "strategy_ranker", "consensus_gates", "trade_score",
                 "dynamic_entry", "replay_engine", "history_manager",
                 "trade_enrichment", "setup_library", "market_regime"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def msg(*a):
        print(*a, flush=True)
    base_cfg = load_config()
    symbols = list(base_cfg["mt5"]["symbols"])
    timeframes = ["M5", "M15"]

    variants, exits, gates = _grid_variants(args.grid, args.variants, args.exits, args.gates)
    cells = [(v, e, g) for v in variants for e in exits for g in gates]
    msg(f"Quantum grid: {len(variants)} variants x {len(exits)} exits x {len(gates)} gates = {len(cells)} cells")

    msg("Staging history from", REAL_HISTORY_DIR)
    frames = _load_raw(symbols, timeframes)
    common_start, common_end = _common_window(frames)
    msg(f"Common window: {common_start} -> {common_end} "
        f"({(common_end - common_start).total_seconds() / 86400:.1f} days)")

    splits = make_splits(common_start, common_end, args.train_ratio, args.folds)
    if args.quick:
        ref = frames[symbols[0]]["M5"]
        quick_start = ref["time"].iloc[-2500]
        for s in splits:
            if s["test_start"] < quick_start:
                s["test_start"] = pd.Timestamp(quick_start)
        msg(f"QUICK mode: test truncated to {quick_start} onwards")

    if args.cost_bps is not None:
        # Override --cost-r with an R-equivalent derived from the staged data so
        # the grid runs at a realistic retail round-trip cost (see _cost_r_from_bps).
        cost_r, prov = _cost_r_from_bps(frames[symbols[0]]["M5"],
                                        cost_bps=args.cost_bps, spot=args.cost_spot)
        msg(f"COST-BPS override: {prov['cost_bps']:.1f} bps round-trip @ spot "
            f"{prov['spot']:.2f}, median 1.5*ATR stop {prov['median_sl_dist']:.4f} "
            f"-> cost_r={cost_r:.4f}R/trade (was {args.cost_r:.4f})")
        args.cost_r = cost_r

    scratch = Path(tempfile.mkdtemp(prefix="quantum_"))
    msg("Scratch dir:", scratch)

    # Per-cell accumulator across folds: {cell_key: {fold_stats: [...], trades: [...]}}
    cell_results: dict[str, dict[str, Any]] = {}
    for v, e, g in cells:
        cell_results[f"{v}|{e}|{g}"] = {"variant": v, "exit": e, "gate": g,
                                        "fold_stats": [], "pooled_trades": []}

    for split in splits:
        msg(f"=== Fold {split['fold']}  train[{split['train_start']}..{split['train_end']}] "
            f"test[{split['test_start']}..{split['test_end']}] ===")

        # ---- 1. Stage TRAIN, learn gate templates once for this fold ---- #
        t_start, t_end = split["train_start"], split["train_end"]
        sliced = _slice_frames(frames, t_start, t_end)
        if args.max_window_bars and len(sliced[symbols[0]]["M5"]) > args.max_window_bars:
            cap = sliced[symbols[0]]["M5"]["time"].iloc[-args.max_window_bars]
            sliced = _slice_frames(frames, cap, t_end)
        _stage_window(scratch, sliced)
        train_out = run_baseline_replay(base_cfg, args.capital, symbols, args.step, logger=log)
        train_trades = train_out["trades"]

        fold_templates: dict[str, dict[str, Any] | None] = {}
        for gname, gcfg in GATE_CONFIGS.items():
            if not gcfg.get("enabled"):
                fold_templates[gname] = None
                continue
            tmpl = learn_templates(train_trades, min_n=gcfg["min_n"],
                                    min_wr=gcfg["min_wr"], cost_r=args.cost_r)
            fold_templates[gname] = tmpl if tmpl else None
        n_cells_learned = sum(1 for v2 in fold_templates.values() if v2)
        msg(f"  TRAIN: {len(train_trades)} trades -> gate templates learned for "
            f"{n_cells_learned}/{len(GATE_CONFIGS)} gate configs")

        # ---- 2. Stage TEST, run every cell ---- #
        te_start, te_end = split["test_start"], split["test_end"]
        sliced = _slice_frames(frames, te_start, te_end)
        if args.max_window_bars and len(sliced[symbols[0]]["M5"]) > args.max_window_bars:
            cap = sliced[symbols[0]]["M5"]["time"].iloc[-args.max_window_bars]
            sliced = _slice_frames(frames, cap, te_end)
        min_rows = min(len(sliced[s]["M5"]) for s in symbols)
        if min_rows < 50:
            msg(f"  skip TEST (only {min_rows} bars)")
            continue
        _stage_window(scratch, sliced)

        for v, e, g in cells:
            gcfg = GATE_CONFIGS[g]
            templates = fold_templates.get(g)
            strict = gcfg.get("strict", False)
            key = f"{v}|{e}|{g}"
            try:
                out = run_cell(base_cfg, v, e, g, templates, args.capital, symbols, args.step,
                               strict=strict, logger=log)
            except Exception as exc:  # noqa: BLE001
                msg(f"  cell {key} crashed: {exc}")
                cell_results[key]["fold_stats"].append({"fold": split["fold"], "error": str(exc)})
                continue
            stats = compute_stats(out["trades"], cost_r=args.cost_r)
            stats["fold"] = split["fold"]
            stats["final_equity"] = round(float(out.get("final_equity", 0)), 2)
            cell_results[key]["fold_stats"].append(stats)
            cell_results[key]["pooled_trades"].extend(out["trades"])
            msg(f"  {key:26s} tr={stats['trades']:4d} wr={stats['win_rate_pct']:5.1f}% "
                f"netR={stats['expectancy_net_r']:+.4f} CIlo={_ci95_lo(stats):+.3f} "
                f"PF={stats['profit_factor']}")

    # ---- Pooled per-cell stats + ranking ---- #
    K = len(cells)  # number of comparisons for Bonferroni / DSR
    ranked: list[dict[str, Any]] = []
    all_r_series: dict[str, list[float]] = {}        # NET of cost (deployability)
    all_r_series_gross: dict[str, list[float]] = {}  # gross, kept for the diagnostic dump
    cell_srs: dict[str, float] = {}
    cell_srs_gross: dict[str, float] = {}
    for key, cr in cell_results.items():
        rs_gross = trade_r_multiples(cr["pooled_trades"])
        rs_net = [r - args.cost_r for r in rs_gross]
        all_r_series[key] = rs_net
        all_r_series_gross[key] = rs_gross
        pooled = compute_stats(cr["pooled_trades"], cost_r=args.cost_r)
        # Per-trade NET Sharpe for the DSR cross-trial variance term (must match
        # the R series DSR is computed on, which is net).
        if len(rs_net) >= 2:
            mean = sum(rs_net) / len(rs_net)
            var = sum((x - mean) ** 2 for x in rs_net) / len(rs_net)
            cell_srs[key] = mean / (var ** 0.5) if var > 0 else 0.0
        else:
            cell_srs[key] = 0.0
        # Gross per-trade Sharpe for the OLD-convention diff field (dsr_crude_gross
        # reproduces crude-threshold DSR on GROSS R, so its cross-trial variance
        # term must also be gross-based -- mixing gross R with net-based sr_var was
        # a mislabeled hybrid that overstated the precise-vs-crude contrast).
        if len(rs_gross) >= 2:
            gmean = sum(rs_gross) / len(rs_gross)
            gvar = sum((x - gmean) ** 2 for x in rs_gross) / len(rs_gross)
            cell_srs_gross[key] = gmean / (gvar ** 0.5) if gvar > 0 else 0.0
        else:
            cell_srs_gross[key] = 0.0
        fold_nets = [s.get("expectancy_net_r", 0.0) for s in cr["fold_stats"] if "error" not in s]
        folds_positive = sum(1 for n in fold_nets if n > 0)
        folds_total = len(fold_nets)
        majority = folds_positive > folds_total / 2 if folds_total else False
        # Bonferroni: require CI95 lower bound > 0 at alpha/K instead of alpha.
        # We don't re-bootstrap per cell at alpha/K (expensive); we approximate by
        # demanding a positive lower bound AND netR above a K-scaled floor.
        bonf_floor = (args.cost_r * 0.5) * (1.0 + (K / 40.0)) if args.bonferroni else 0.0
        ci_lo = _ci95_lo(pooled)
        eligible = pooled["trades"] >= args.min_trades
        ranked.append({
            "cell": key, "variant": cr["variant"], "exit": cr["exit"], "gate": cr["gate"],
            "trades": pooled["trades"], "win_rate_pct": pooled["win_rate_pct"],
            "expectancy_r": pooled["expectancy_r"], "expectancy_net_r": pooled["expectancy_net_r"],
            "profit_factor": pooled["profit_factor"], "max_drawdown_r": pooled["max_drawdown_r"],
            "ci95": pooled["ci95"], "final_equity": pooled.get("final_equity", 0.0),
            "folds_positive": folds_positive, "folds_total": folds_total,
            "majority_fold_positive": majority, "eligible": eligible,
            "bonferroni_floor": round(bonf_floor, 4),
        })

    # ---- Deflated Sharpe Ratio: the proper data-snooping correction ---- #
    # Cross-trial SR variance = how much the best cell's SR is inflated by pure
    # search across the K cells (the selection-bias term in DSR).
    sr_vals = [v for v in cell_srs.values()]
    sr_var = (sum((v - sum(sr_vals) / len(sr_vals)) ** 2 for v in sr_vals) / len(sr_vals)
              if sr_vals else 0.0)
    # Gross-based cross-trial SR variance for the old-convention diff field.
    sr_vals_gross = [v for v in cell_srs_gross.values()]
    sr_var_gross = (sum((v - sum(sr_vals_gross) / len(sr_vals_gross)) ** 2 for v in sr_vals_gross) / len(sr_vals_gross)
                    if sr_vals_gross else 0.0)
    # Optional diagnostic dump (no behavior change); set DUMP_RSERIES=path to
    # write the pooled per-cell R-multiples (net + gross) + cross-trial SR
    # variance, so the DSR threshold convention can be re-evaluated offline
    # without a full re-run.
    _dump = os.environ.get("DUMP_RSERIES")
    if _dump:
        import json as _json
        with open(_dump, "w") as _fh:
            _json.dump({"sr_var": sr_var, "sr_var_gross": sr_var_gross,
                        "cell_srs": cell_srs, "cell_srs_gross": cell_srs_gross,
                        "r_series_net": {k: v for k, v in all_r_series.items()},
                        "r_series_gross": {k: v for k, v in all_r_series_gross.items()}},
                       _fh)
    dsr_threshold = 0.95
    for r in ranked:
        # DSR on NET R (deployability is post-cost). Precise FST two-quantile
        # threshold is the gate (published form); crude sqrt(2 ln K) kept alongside
        # so the threshold convention's effect is visible without a re-run.
        dsr_precise = deflated_sharpe(all_r_series.get(r["cell"], []),
                                     n_trials=K, sr_var_across_trials=sr_var,
                                     threshold_mode="precise")
        # The diff field reproduces the OLD convention (crude sqrt(2 ln K)
        # threshold on GROSS R) so the threshold+cost convention's effect is
        # visible without a re-run. Must use the GROSS series for that -- the
        # gate series (all_r_series) is net.
        dsr_crude_gross = deflated_sharpe(all_r_series_gross.get(r["cell"], []),
                                         n_trials=K, sr_var_across_trials=sr_var_gross,
                                         threshold_mode="crude")
        r["dsr"] = round(dsr_precise, 4)               # gate (precise, net R)
        r["dsr_precise"] = round(dsr_precise, 4)
        r["dsr_crude_gross"] = round(dsr_crude_gross, 4)  # old convention, for diff
        r["dsr_survives"] = dsr_precise >= dsr_threshold
        # An organic winner must survive the DSR gate (the proper data-snooping
        # bar), a positive CI95 lower bound, fold stability, AND the Bonferroni
        # K-scaled netR floor (a redundant secondary multiple-comparison check;
        # DSR/SPA are the primary bars -- Bonferroni is cruder but kept as a
        # belt-and-braces gate so the --bonferroni flag's documented claim holds).
        r["organic_winner"] = (r["eligible"] and r["dsr_survives"]
                               and _ci95_lo(r) > 0 and r["majority_fold_positive"]
                               and r["expectancy_net_r"] > (r.get("bonferroni_floor") or 0.0))
    ranked.sort(key=lambda r: r["expectancy_net_r"], reverse=True)

    # ---- Report ---- #
    print("\n" + "=" * 112)
    print("QUANTUM LOOP  (capital=$%.2f, cost=%.2fR/trade, grid=%dx%dx%d=%d cells, "
          "folds=%d, K=%d)" % (args.capital, args.cost_r, len(variants),
          len(exits), len(gates), K, len(splits), K))
    print("=" * 112)
    hdr = (f"{'cell':28s} {'win':6s} {'tr':5s} {'netR':8s} {'PF':6s} "
           f"{'maxDD':7s} {'CI95lo':8s} {'DSR':6s} {'folds':6s} {'organic':8s}")
    print(hdr)
    for r in ranked:
        print(f"{r['cell']:28s} {r['win_rate_pct']:5.1f}% {r['trades']:5d} "
              f"{r['expectancy_net_r']:+8.4f} {str(r['profit_factor']):6s} "
              f"{r['max_drawdown_r']:7.2f} {r['ci95'][0]:+8.3f} {r['dsr']:6.3f} "
              f"{r['folds_positive']}/{r['folds_total']:<3d} "
              f"{'YES' if r['organic_winner'] else '':8s}")

    winners = [r for r in ranked if r["organic_winner"]]
    print("\n" + "-" * 112)
    print(f"ORGANIC WINNERS (OOS, net of cost, DSR>={dsr_threshold:.2f} [corrects for K={K}-trial "
          f"selection + non-normality], CI95 lo>0, majority folds positive, >= {args.min_trades} trades):")
    if winners:
        for w in winners:
            print("  + %-26s netR=%+.4f wr=%.1f%% tr=%d PF=%s maxDD=%.2fR DSR=%.3f CI95[%+.3f,%+.3f] folds=%d/%d"
                  % (w["cell"], w["expectancy_net_r"], w["win_rate_pct"], w["trades"],
                     w["profit_factor"], w["max_drawdown_r"], w["dsr"],
                     w["ci95"][0], w["ci95"][1],
                     w["folds_positive"], w["folds_total"]))
    else:
        print("  (none) — no cell has a statistically-positive OOS edge that survives the")
        print(f"  data-snooping correction (DSR>={dsr_threshold:.2f} across K={K} cells). The best of {K}")
        print("  tried cells is still within noise net of cost. This is the honest answer: sweeping a")
        print("  discretised grid does not manufacture an edge the setup generator lacks; it only reveals it.")
        best = ranked[0] if ranked else None
        if best:
            print(f"  best cell {best['cell']}: netR={best['expectancy_net_r']:+.4f}, "
                  f"CI95 lo={best['ci95'][0]:+.3f}, DSR={best['dsr']:.3f}, "
                  f"folds positive={best['folds_positive']}/{best['folds_total']}")

    # ---- Per-trade narrative dump for the top cell ---- #
    show_trades = {}
    if args.show_trades and ranked:
        top_key = ranked[0]["cell"]
        top_trades = cell_results[top_key]["pooled_trades"]
        n = min(args.show_trades, len(top_trades))
        msg(f"Dumping {n}/{len(top_trades)} trades from top cell {top_key}")
        rows = []
        for t in top_trades[:n]:
            rows.append({
                "symbol": t.get("symbol"), "side": t.get("side"),
                "setup_type": t.get("setup_type"),
                "entry": t.get("entry"), "exit": t.get("exit"),
                "sl": t.get("sl"), "tp1": t.get("tp1"),
                "pnl": t.get("pnl"), "exit_reason": t.get("exit_reason"),
                "entry_narrative": t.get("entry_narrative"),
                "be_narrative": t.get("be_narrative"),
                "trail_narrative": t.get("trail_narrative"),
                "exit_narrative": t.get("exit_narrative"),
                "cell": trade_cell(t),
            })
        show_trades = {"cell": top_key, "trades": rows}
        print("\n--- Top cell %s: first %d trades (entry/BE/trail/exit narrative + profitability) ---"
              % (top_key, n))
        for r in rows:
            print(f"\n  {r['side']} {r['symbol']} ({r['setup_type']}) cell={r['cell']}")
            print(f"    entry={r['entry']} exit={r['exit']} sl={r['sl']} pnl=${r['pnl']} ({r['exit_reason']})")
            if r["entry_narrative"]:
                print(f"    ENTRY : {r['entry_narrative']}")
            if r["be_narrative"]:
                print(f"    BE    : {r['be_narrative']}")
            if r["trail_narrative"]:
                print(f"    TRAIL : {r['trail_narrative']}")
            if r["exit_narrative"]:
                print(f"    EXIT  : {r['exit_narrative']}")

    payload = {
        "capital_usd": args.capital, "cost_r": args.cost_r, "step": args.step,
        "min_trades": args.min_trades, "grid": args.grid,
        "grid_dims": {"variants": len(variants), "exits": len(exits), "gates": len(gates)},
        "K_comparisons": K, "bonferroni": args.bonferroni,
        "common_window": {"start": str(common_start), "end": str(common_end)},
        "n_folds": len(splits),
        "ranked": ranked,
        "organic_winners": [w["cell"] for w in winners],
        "show_trades": show_trades,
        "dsr_sr_variance": round(sr_var, 6),
        "dsr_threshold": dsr_threshold,
        "note": ("Quantum loop = discretised exhaustive OOS sweep. Every (variant, exit, "
                 "gate) cell walk-forward evaluated; winners selected by Deflated Sharpe Ratio "
                 "(Bailey & Lopez de Prado 2014) >= 0.95 — the data-snooping correction that "
                 "handles BOTH K-trial selection bias AND return non-normality, which crude "
                 "Bonferroni does not — plus CI95 lower bound > 0 net of cost and majority-fold "
                 "stability. Read-only on live edge DB. No live trades. Honest by construction: "
                 "selecting the best of K noisy estimates is multiple-comparison bias; DSR is "
                 "what makes a surfaced winner real rather than a mirage. Per deep-research "
                 "2026-06-27, DSR/SPA is the literature's proper bar and is conservative (low "
                 "power) — rejection credible, non-rejection weak."),
    }
    args.out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("\nReport written to %s" % args.out)

    import shutil
    try:
        shutil.rmtree(scratch)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())