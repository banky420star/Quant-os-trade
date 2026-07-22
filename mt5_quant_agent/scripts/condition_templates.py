"""Win-condition template learner + honest out-of-sample evaluator.

The user's idea, implemented faithfully and tested honestly:

    "it should be saving the conditions for the [~40% of] times it won and only
     using those conditions, then using other strategies and doing the same."

This learns, on a TRAIN window, the ``(setup_type x regime x bias-aligned)``
condition CELLS where the bot historically WON, then gates TEST-window entries
to only those cells, and reports whether the gate produces a statistically-
positive OOS edge (net of cost, CI95 lower bound > 0, >= min_trades).

Why the cell is ``setup | regime | bias-aligned``
-------------------------------------------------
A "condition" the bot can recognise at entry time, computed from the same
signal fields the verifier sees: the setup_type, the market_regime primary
label the engine already classifies per bar, and whether the trade's side
agrees with the regime bias. Every dimension is available live, so the gate
is deployable, not just backtest-only.

Honesty guards (the part that makes this worth running, not just plausible)
--------------------------------------------------------------------------
1. OUT OF SAMPLE. Templates are learned on TRAIN only and frozen. TEST is
   unseen. The whole point is to check whether in-sample winning cells hold
   out-of-sample.
2. SELECTION BIAS IS ACKNOWLEDGED. Selecting cells by in-sample win rate IS
   multiple-comparison bias — with many cells, some will look good by chance.
   The OOS TEST is the correction: if selected cells don't hold OOS, they
   were period-specific noise, not an edge. We report both.
3. WALK-FORWARD. ``--folds>1`` tests whether winning conditions are stable
   across regimes or fold-specific (the static-strategy failure mode).
4. READ-ONLY on the live edge DB (``replay_ingest_edge_db=False`` via
   build_variant_config). No live trades, no live state mutation.
5. SAME baseline pipeline. The gated run is the shipped config + one opt-in
   gate, not a parallel engine — so an OOS edge here is deployable as-is.

Usage
-----
    python scripts/condition_templates.py --quick          # smoke test
    python scripts/condition_templates.py                 # single split
    python scripts/condition_templates.py --folds 4       # walk-forward
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.replay_engine import run_portfolio_replay  # noqa: E402
from core.strategy_variants import build_variant_config  # noqa: E402
from core.utils import load_config, setup_logger  # noqa: E402

# Reuse the evaluator's tested helpers instead of duplicating them.
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


# --------------------------------------------------------------------------- #
# Condition cell.                                                              #
# --------------------------------------------------------------------------- #

def _cell_key(setup_type: str | None, regime_primary: str | None,
             regime_bias: str | None, side: str | None) -> str:
    """Cell the verifier gates on. MUST match verifier._verify_one exactly."""
    aligned = (
        (regime_bias in ("bullish", "up") and side == "BUY")
        or (regime_bias in ("bearish", "down") and side == "SELL")
    )
    return f"{setup_type or 'unknown'}|{regime_primary or '?'}|{'align' if aligned else 'counter'}"


def trade_cell(t: dict[str, Any]) -> str:
    mc = t.get("market_context") or {}
    reg = mc.get("market_regime") or {}
    return _cell_key(t.get("setup_type"), reg.get("primary"), reg.get("bias"), t.get("side"))


def _trade_r(t: dict[str, Any]) -> float | None:
    """Single-trade R (capital-independent). Mirrors trade_r_multiples."""
    try:
        entry = float(t.get("entry", 0) or 0)
        exitp = float(t.get("exit", 0) or 0)
        sl = float(t.get("sl", 0) or 0)
        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            return None
        side = str(t.get("side", "")).upper()
        dprice = (exitp - entry) if side != "SELL" else (entry - exitp)
        return dprice / sl_dist
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Template learning + gated replay.                                           #
# --------------------------------------------------------------------------- #

def learn_templates(
    train_trades: list[dict[str, Any]],
    *,
    min_n: int,
    min_wr: float,
    cost_r: float,
) -> dict[str, dict[str, Any]]:
    """Build per-setup_type allowed cells from TRAIN trades.

    A cell is allowed iff it has >= ``min_n`` trades, win-rate >= ``min_wr``,
    and positive net expectancy per trade (R minus cost). Returns
    ``{setup_type: {cell_key: {n, win_rate, expR, netR}}}`` for allowed cells
    only — the shape the verifier's ``win_templates`` config expects.
    """
    # Group R + result by (setup_type, cell).
    cells: dict[str, dict[str, list]] = {}
    for t in train_trades:
        setup = t.get("setup_type") or "unknown"
        cell = trade_cell(t)
        r = _trade_r(t)
        if r is None:
            continue
        cells.setdefault(setup, {}).setdefault(cell, []).append(
            (r, t.get("result") == "win")
        )

    templates: dict[str, dict[str, Any]] = {}
    for setup, cell_map in cells.items():
        allowed: dict[str, Any] = {}
        for cell, recs in cell_map.items():
            n = len(recs)
            wins = sum(1 for _, w in recs if w)
            wr = 100.0 * wins / n
            exp_r = sum(r for r, _ in recs) / n
            net_r = exp_r - cost_r
            if n >= min_n and wr >= min_wr and net_r > 0:
                allowed[cell] = {
                    "n": n, "win_rate_pct": round(wr, 1),
                    "expectancy_r": round(exp_r, 4), "net_r": round(net_r, 4),
                }
        if allowed:
            templates[setup] = allowed
    return templates


def run_replay(
    base_cfg: dict[str, Any],
    capital: float,
    symbols: list[str],
    step: int,
    *,
    win_templates: dict[str, Any] | None = None,
    strict: bool = False,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Baseline config (+ optional win-template gate) -> portfolio replay."""
    cfg = build_variant_config(base_cfg, "baseline", capital_usd=capital)
    if win_templates is not None:
        cfg["signals"]["win_templates"] = win_templates
        cfg["signals"]["win_template_strict"] = strict
    return run_portfolio_replay(
        cfg, symbols,
        max_bars=10_000_000, step=step, logger=logger,
        spread_data=LIVE_SPREAD_POINTS, return_all_trades=True,
    )


def _stats_with_cells(trades: list[dict[str, Any]], *, cost_r: float) -> dict[str, Any]:
    s = compute_stats(trades, cost_r=cost_r)
    s["cells_seen"] = len({trade_cell(t) for t in trades})
    return s


# --------------------------------------------------------------------------- #
# Main.                                                                       #
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Win-condition template learner + OOS evaluator")
    ap.add_argument("--capital", type=float, default=80.0)
    ap.add_argument("--train-ratio", type=float, default=0.7)
    ap.add_argument("--folds", type=int, default=1)
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--cost-r", type=float, default=0.15)
    ap.add_argument("--min-trades", type=int, default=40, help="min TEST trades for a verdict")
    ap.add_argument("--min-n", type=int, default=4, help="min trades per cell to be kept as a template")
    ap.add_argument("--min-wr", type=float, default=50.0, help="min cell win-rate %% to be kept")
    ap.add_argument("--strict", action="store_true",
                    help="reject setups with no learned template (else allow them)")
    ap.add_argument("--max-window-bars", type=int, default=10000)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", type=Path, default=ROOT / "condition_template_report.json")
    args = ap.parse_args()

    log = setup_logger("condition_templates", "condition_templates.log")
    base_cfg = load_config()
    symbols = list(base_cfg["mt5"]["symbols"])
    timeframes = ["M5", "M15"]

    log.info("Staging history from %s", REAL_HISTORY_DIR)
    frames = _load_raw(symbols, timeframes)
    common_start, common_end = _common_window(frames)
    log.info("Common window: %s -> %s (%.1f days)",
             common_start, common_end,
             (common_end - common_start).total_seconds() / 86400)

    splits = make_splits(common_start, common_end, args.train_ratio, args.folds)
    if args.quick:
        ref = frames[symbols[0]]["M5"]
        quick_start = ref["time"].iloc[-2500]
        for s in splits:
            if s["test_start"] < quick_start:
                s["test_start"] = pd.Timestamp(quick_start)
        log.info("QUICK mode: test truncated to %s onwards", quick_start)

    scratch = Path(tempfile.mkdtemp(prefix="cond_tmpl_"))
    log.info("Scratch dir: %s", scratch)

    fold_rows: list[dict[str, Any]] = []
    pooled: dict[str, list[dict[str, Any]]] = {"baseline": [], "templated": []}

    for split in splits:
        log.info("=== Fold %d  train[%s..%s] test[%s..%s] ===",
                 split["fold"], split["train_start"], split["train_end"],
                 split["test_start"], split["test_end"])

        # ---- 1. Learn templates on TRAIN ---- #
        t_start, t_end = split["train_start"], split["train_end"]
        sliced = _slice_frames(frames, t_start, t_end)
        if args.max_window_bars and len(sliced[symbols[0]]["M5"]) > args.max_window_bars:
            cap = sliced[symbols[0]]["M5"]["time"].iloc[-args.max_window_bars]
            sliced = _slice_frames(frames, cap, t_end)
        _stage_window(scratch, sliced)
        train_out = run_replay(base_cfg, args.capital, symbols, args.step, logger=log)
        train_trades = train_out["trades"]
        templates = learn_templates(train_trades, min_n=args.min_n, min_wr=args.min_wr,
                                    cost_r=args.cost_r)
        n_cells = sum(len(v) for v in templates.values())
        train_stats = compute_stats(train_trades, cost_r=args.cost_r)
        log.info("  TRAIN: %d trades, wr=%.1f%%, netR=%.4f -> learned %d allowed cells across %d setups",
                 train_stats["trades"], train_stats["win_rate_pct"],
                 train_stats["expectancy_net_r"], n_cells, len(templates))
        if not templates:
            log.info("  (no cells met min_n=%d / min_wr=%.0f%% / netR>0 — gate will be a no-op)",
                     args.min_n, args.min_wr)

        # ---- 2. Evaluate TEST: baseline (no gate) vs templated (gated) ---- #
        te_start, te_end = split["test_start"], split["test_end"]
        sliced = _slice_frames(frames, te_start, te_end)
        if args.max_window_bars and len(sliced[symbols[0]]["M5"]) > args.max_window_bars:
            cap = sliced[symbols[0]]["M5"]["time"].iloc[-args.max_window_bars]
            sliced = _slice_frames(frames, cap, te_end)
        min_rows = min(len(sliced[s]["M5"]) for s in symbols)
        if min_rows < 50:
            log.info("  skip TEST (only %d bars)", min_rows)
            continue
        _stage_window(scratch, sliced)

        base_out = run_replay(base_cfg, args.capital, symbols, args.step,
                              win_templates=None, logger=log)
        base_stats = _stats_with_cells(base_out["trades"], cost_r=args.cost_r)
        log.info("  TEST baseline:   trades=%d wr=%.1f%% netR=%.4f PF=%s cells=%d",
                 base_stats["trades"], base_stats["win_rate_pct"],
                 base_stats["expectancy_net_r"], base_stats["profit_factor"],
                 base_stats["cells_seen"])

        tmpl_out = run_replay(base_cfg, args.capital, symbols, args.step,
                              win_templates=templates, strict=args.strict, logger=log)
        tmpl_stats = _stats_with_cells(tmpl_out["trades"], cost_r=args.cost_r)
        log.info("  TEST templated:  trades=%d wr=%.1f%% netR=%.4f PF=%s cells=%d  (gate kept %.0f%% of baseline trades)",
                 tmpl_stats["trades"], tmpl_stats["win_rate_pct"],
                 tmpl_stats["expectancy_net_r"], tmpl_stats["profit_factor"],
                 tmpl_stats["cells_seen"],
                 100.0 * tmpl_stats["trades"] / base_stats["trades"] if base_stats["trades"] else 0.0)

        pooled["baseline"].extend(base_out["trades"])
        pooled["templated"].extend(tmpl_out["trades"])
        fold_rows.append({
            "fold": split["fold"],
            "train_trades": train_stats["trades"], "train_net_r": train_stats["expectancy_net_r"],
            "cells_learned": n_cells,
            "test_baseline": base_stats, "test_templated": tmpl_stats,
        })

    # ---- Pooled OOS verdict ---- #
    print("\n" + "=" * 92)
    print("WIN-CONDITION TEMPLATE EVALUATOR  (capital=$%.2f, cost=%.2fR/trade, "
          "min_n=%d, min_wr=%.0f%%, strict=%s)" % (args.capital, args.cost_r,
          args.min_n, args.min_wr, args.strict))
    print("=" * 92)
    print(f"\n{'':14s} {'win':6s} {'tr':6s} {'expR':8s} {'netR':8s} {'PF':6s} {'maxDD':7s} {'CI95':>16s} {'cells':6s}")
    pooled_stats: dict[str, dict[str, Any]] = {}
    for label in ("baseline", "templated"):
        s = _stats_with_cells(pooled[label], cost_r=args.cost_r)
        pooled_stats[label] = s
        print(f"{label:14s} {s['win_rate_pct']:5.1f}% {s['trades']:6d} "
              f"{s['expectancy_r']:8.4f} {s['expectancy_net_r']:8.4f} "
              f"{str(s['profit_factor']):6s} {s['max_drawdown_r']:7.2f} "
              f"[{s['ci95'][0]:+.3f},{s['ci95'][1]:+.3f}] {s['cells_seen']:6d}")

    b, t = pooled_stats["baseline"], pooled_stats["templated"]
    print("\n" + "-" * 92)
    edge = (t["trades"] >= args.min_trades and t["expectancy_net_r"] > 0 and t["ci95"][0] > 0)
    beats = t["expectancy_net_r"] > b["expectancy_net_r"]
    print("VERDICT (pooled TEST, >= %d trades, CI95 lower bound > 0):" % args.min_trades)
    print(f"  templated OOS edge: {'YES' if edge else 'NO'}  "
          f"(netR={t['expectancy_net_r']:+.4f}, CI95 lo={t['ci95'][0]:+.3f}, trades={t['trades']})")
    print(f"  gate beats baseline on net expectancy: {'YES' if beats else 'NO'}  "
          f"(templated {t['expectancy_net_r']:+.4f} vs baseline {b['expectancy_net_r']:+.4f})")
    if t["trades"] < args.min_trades:
        print(f"  NOTE: only {t['trades']} templated trades — below min_trades; "
              f"verdict is NOT statistically meaningful. The gate is too sparse "
              f"on this history to validate the idea either way.")
    if edge and beats:
        print("  -> Real conditional edge. Deploy the win-template gate live "
              "(templates learned from full history, relearned periodically).")
    elif beats and t["expectancy_net_r"] > 0:
        print("  -> Gate improves expectancy but CI still includes 0; more data needed.")
    else:
        print("  -> Winning conditions from TRAIN did NOT hold OOS. The ~40% win "
              "rate is period-specific noise, not a regime-conditional edge. "
              "Restricting to 'winning cells' cannot manufacture an edge the "
              "setup generator does not have; the fix is upstream (setup/signal "
              "generation), not condition-gating.")

    payload = {
        "capital_usd": args.capital, "cost_r": args.cost_r,
        "min_n": args.min_n, "min_wr": args.min_wr, "strict": args.strict,
        "common_window": {"start": str(common_start), "end": str(common_end)},
        "folds": fold_rows,
        "pooled": pooled_stats,
        "verdict": {"edge": edge, "beats_baseline": beats,
                    "templated_trades": t["trades"], "min_trades": args.min_trades},
        "note": ("Condition cell = setup|regime|bias-aligned. Templates learned "
                 "on TRAIN, frozen, evaluated OOS on TEST. Read-only on live edge "
                 "DB. No live trades."),
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