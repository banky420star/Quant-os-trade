"""Walk-forward gate for live config patches.

Takes a proposal JSON in the same shape as ``calibrate_be_trail.py`` /
``tune_persymbol_be.py`` output:

    {
      "symbols": {
        "XAUUSDm": {
          "break_even": {"trigger_atr_mult": 0.3, "lock_profit_atr_mult": 0.05},
          "trailing":  {"activation_atr_mult": 0.5, "trail_atr_mult": 0.25}
        },
        ...
      }
    }

Loads the last ``--tail-n`` trades per symbol (default 100), splits 70/30
chronologically, and replays the 30% tail BOTH under the active baseline
(defaults from ``state/symbol_be_trail_live.json`` → ``config.yaml`` seeds)
AND under the proposed params. Compares via ``calibrate_be_trail._exit``
(MAE/MFE in R units, identical simulation model — see caveat below).

Verdict (must pass BOTH conditions across the 30% tail):
  * aggregate ``net_R_delta`` > 0     (proposed sums to more R than baseline)
  * aggregate ``expectancy_delta`` > 0 (trade-weighted mean R improved, not just count)

Plus minimums: at least ``--min-symbols`` distinct symbols (default 3) and
each must have ``--min-tail`` trades in the 30% window (default 5). A symbol
whose MAE/MFE coverage is too thin (< ``--max-interp-frac`` of tail, default
0.5) is excluded from the aggregate so it cannot be carried by
interpolated (=0-delta) rows. Symbols that lack either condition are listed
under "insufficient_data" WITH a per-symbol reason
({"symbol": ..., "reason": "interp_frac=0.83>0.5", "n_tail_raw": 30, ...}).

EFFECTIVE MINIMUM: with defaults ``min_tail=5`` + ``max_interp_frac=0.5``,
the lowest a symbol can pass with is ``ceil(5 * 0.5) = 3`` evaluated rows
(surfaced in the report as ``aggregate.effective_min_evaluated_per_symbol``).
Trade the constants off: tighten ``--max-interp-frac`` to 0.3 if your
trade log has rich MAE/MFE; loosen to 0.7 if it's still gappy and you'd
rather evaluate than refuse.

CAVEAT — model optimism. ``calibrate_be_trail._exit`` assumes the favorable
peak is reached BEFORE the adverse trough ("good-then-bad") because we only
have maxima/minima from the trade log. This is OPTIMISTIC for trailing, and
both the proposal AND the baseline are replayed under the SAME model — so the
delta is fair inside-model but trades a model of real prices. We log a
warning so the operator knows the verdict is only as good as the simulation.

EXIT CODES:
    0 = pass (writes proposal to state/symbol_be_trail_live.json
              ONLY when --apply is also passed)
    1 = fail (proposal kept in output report; live state unchanged)
    2 = insufficient data (too few trades / symbols OR too many interpolated)
    3 = invalid proposal schema

This script DOES NOT auto-write to live state. It records the verdict in
the output JSON and uses --apply to opt into a write on PASS only. That
separation means CI / cron / humans can read the report before deciding.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import (  # noqa: E402
    read_json_state,
    setup_logger,
    utc_now_iso,
    write_json_state,
)
from scripts.calibrate_be_trail import _exit as simulate_exit  # noqa: E402


# Trade-log filtering AND tail-minimums (consistent with calibrate_be_trail.MIN_N).
MIN_SYMBOLS = 3                  # global minimum distinct symbols
MIN_TAIL_PER_SYMBOL = 5          # minimum trades in 30% window per symbol
MIN_OVERALL_TRADES = 30          # minimum trades globally to bother running
MAX_INTERPOLATED_FRACTION = 0.5  # max share of MAE/MFE-missing rows per symbol
DEFAULT_TAIL_N = 100
DEFAULT_SPLIT_PCT = 0.7


def effective_min_evaluated(min_tail: int = MIN_TAIL_PER_SYMBOL,
                            max_interp: float = MAX_INTERPOLATED_FRACTION) -> int:
    """Effective minimum evaluated rows per symbol under given thresholds.

    With the defaults (``min_tail=5``, ``max_interp=0.5``) this is 3. The
    aggregate dict always reports THIS value (recomputed per call), not the
    module-level constant — so the report reflects whatever
    ``--min-tail``/``--max-interp-frac`` the operator actually passed.
    """
    return max(1, math.ceil(min_tail * (1.0 - max_interp)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _trade_sort_key(t: dict[str, Any]) -> str:
    """Sort key: closed_at (preferred) → opened_at → trade_id.

    Strings sort lexically but ISO timestamps ARE lexically sortable for valid
    ranges. Empty string falls to the back so missing timestamps don't break
    ordering — they'll group together but still be deterministic.
    """
    return (
        str(t.get("closed_at") or "") + "|" +
        str(t.get("opened_at") or "") + "|" +
        str(t.get("trade_id") or "")
    )


def _is_clean_trade(t: dict[str, Any]) -> bool:
    return not bool(t.get("archive_polluted"))


def _extract_r_units(t: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None]:
    """Return (mae_R, mfe_R, r_mult, tp1_r). Any may be None if missing OR NaN.

    NaN/missing values are coerced to None so the gate can choose to either
    SKIP the trade (preferred) or fall back to realised r_multiple. The
    simulator gate prefers to EXCLUDE these rows from the aggregate (counted
    in ``interpolated_n``) since their delta is always zero and they can
    silently inflate n_tail without contributing signal.
    """
    def _safe(name: str) -> float | None:
        v = t.get(name)
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    mae = _safe("mae_R")
    mfe = _safe("mfe_R")
    rm = _safe("r_multiple")
    # tp1_R = relative-to-risk distance |tp1 - entry| / |entry - sl|
    e = t.get("entry"); sl = t.get("sl_initial") or t.get("sl"); tp1 = t.get("tp1")
    try:
        orig_r = abs(float(e) - float(sl)) if e is not None and sl is not None else 0
        tp1_r = abs(float(tp1) - float(e)) / orig_r if orig_r > 0 and tp1 is not None else None
    except (TypeError, ValueError):
        tp1_r = None
    return mae, mfe, rm, tp1_r


def split_tail(
    trades: list[dict[str, Any]], tail_n: int = DEFAULT_TAIL_N, split_pct: float = DEFAULT_SPLIT_PCT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Sort by closed_at → take last ``tail_n`` → split (1-split_pct) / split_pct.

    Returns ``(train_set, tail_set)``. The 30% tail is what we replay under
    both baseline and proposed params; ``train_set`` is reserved for future
    variants that want to readapt the proposal baseline.
    """
    sorted_trades = sorted(list(trades), key=_trade_sort_key)
    if len(sorted_trades) > tail_n:
        sorted_trades = sorted_trades[-tail_n:]
    split_index = max(1, min(len(sorted_trades) - 1, int(round(len(sorted_trades) * split_pct))))
    train_set = sorted_trades[:split_index]
    tail_set = sorted_trades[split_index:]
    return train_set, tail_set


def _resolve_baseline_params(
    config: dict[str, Any], live_state: dict[str, Any], symbol: str,
) -> tuple[float, float, float, float]:
    """Resolve (be_trig, be_lock, tr_act, tr_dist) in ATR-mult units.

    Order of precedence, MERGING FIELD-BY-FIELD (a symbol that calibrated only
    BE does not silently lose the BE calibration if trail is missing):
      1. Per-symbol live override fields when present.
      2. ``config.yaml['trading']['break_even']['per_symbol'][SYM]`` then top-level.
      3. Default constants.
    """
    tr = (config.get("trading") or {})
    be = tr.get("break_even") or {}
    tl = tr.get("trailing") or {}

    sym_live = live_state.get("symbols", {}).get(symbol) if isinstance(live_state, dict) else None
    be_live = (sym_live.get("break_even") or {}) if isinstance(sym_live, dict) else {}
    tl_live = (sym_live.get("trailing") or {}) if isinstance(sym_live, dict) else {}

    be_sym_seed = (be.get("per_symbol") or {}).get(symbol, {}) if isinstance(be.get("per_symbol"), dict) else {}
    tl_sym_seed = (tl.get("per_symbol") or {}).get(symbol, {}) if isinstance(tl.get("per_symbol"), dict) else {}

    return (
        float(
            be_live.get("trigger_atr_mult",
                        be_sym_seed.get("trigger_atr_mult", be.get("trigger_atr_mult", 0.5)))
        ),
        float(
            be_live.get("lock_profit_atr_mult",
                        be_sym_seed.get("lock_profit_atr_mult", be.get("lock_profit_atr_mult", 0.1)))
        ),
        float(
            tl_live.get("activation_atr_mult",
                        tl_sym_seed.get("activation_atr_mult", tl.get("activation_atr_mult", 0.75)))
        ),
        float(
            tl_live.get("trail_atr_mult",
                        tl_sym_seed.get("trail_atr_mult", tl.get("trail_atr_mult", 0.35)))
        ),
    )


def _simulate_tail(
    tail_trades: list[dict[str, Any]],
    params: tuple[float, float, float, float],
) -> dict[str, Any]:
    """Replay each trade under (be_trig, be_lock, tr_act, tr_dist).

    Trades missing MAE/MFE are EXCLUDED from net_R/expectancy (their delta is
    always 0 and they can pass the gate on empty evidence). They are still
    counted in ``interpolated_n`` so callers can apply an upper-bound on
    coverage before trusting the verdict.
    """
    be_trig, be_lock, tr_act, tr_dist = params
    baseline_outs: list[float] = []
    interpolated_skipped = 0
    for t in tail_trades:
        mae, mfe, rm, tp1_r = _extract_r_units(t)
        if mae is None or mfe is None:
            interpolated_skipped += 1
            continue
        baseline_outs.append(simulate_exit(mae, mfe, rm, tp1_r, be_trig, be_lock, tr_act, tr_dist))
    if not baseline_outs:
        return {"n_tail": 0, "net_R": 0.0, "expectancy_R": 0.0, "interpolated_n": interpolated_skipped}
    return {
        "n_tail": len(baseline_outs),
        "net_R": float(sum(baseline_outs)),
        "expectancy_R": float(sum(baseline_outs) / len(baseline_outs)),
        "interpolated_n": interpolated_skipped,
    }


def _load_proposal(path: Path) -> dict[str, dict[str, Any]]:
    """Load and minimally validate proposal JSON.

    Expects ``{"symbols": {SYM: {"break_even": {...}, "trailing": {...}}}}``.
    Symbols may omit numbers (they fall back to baseline). Returns a clean
    nested dict with float values ready for ``_resolve_baseline_params``.

    Loud-failure policy: any NaN/Inf/TypeError entry aborts the gate entirely.
    Callers must fix the proposal before re-running; we do NOT silently apply a
    partial proposal because that mixes baseline for some symbols and proposed
    for others.
    """
    if not path.exists():
        raise FileNotFoundError(f"proposal not found at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "symbols" not in raw or not isinstance(raw["symbols"], dict):
        raise ValueError("proposal must have a top-level 'symbols' object")
    cleaned: dict[str, dict[str, Any]] = {}
    bad: list[str] = []
    for sym, entry in raw["symbols"].items():
        if not isinstance(entry, dict):
            bad.append(sym)
            continue
        be = entry.get("break_even") or {}
        tl = entry.get("trailing") or {}
        try:
            be_trig = float(be["trigger_atr_mult"])
            be_lock = float(be["lock_profit_atr_mult"])
            tr_act = float(tl["activation_atr_mult"])
            tr_dist = float(tl["trail_atr_mult"])
        except (KeyError, TypeError, ValueError):
            bad.append(sym)
            continue
        if any(math.isnan(x) or math.isinf(x) for x in (be_trig, be_lock, tr_act, tr_dist)):
            bad.append(sym)
            continue
        # Reject obviously-broken numeric ranges (e.g. negative BE triggers,
        # zero trail distances, trailing activation BELOW the BE trigger —
        # the simulator can never trail in that case). Loud-failure policy
        # covers logical nonsense as well as NaN/Inf/TypeError.
        if not (0.0 < be_trig < 10.0 and 0.0 <= be_lock < be_trig
                and 0.0 < tr_act < 10.0 and 0.0 < tr_dist < tr_act
                and tr_act > be_trig):
            bad.append(sym)
            continue
        cleaned[sym] = {
            "break_even": {"trigger_atr_mult": be_trig, "lock_profit_atr_mult": be_lock},
            "trailing": {"activation_atr_mult": tr_act, "trail_atr_mult": tr_dist},
        }
    if not cleaned:
        raise ValueError(f"no usable symbols in proposal; bad entries: {bad}")
    if bad:
        raise ValueError(
            f"proposal has invalid symbols (Nan/Inf/TypeError): {bad}; "
            "fix before re-running."
        )
    return cleaned


# ---------------------------------------------------------------------------
# Main gate logic
# ---------------------------------------------------------------------------
def gate_check(
    trade_log: dict[str, Any],
    config: dict[str, Any],
    live_state: dict[str, Any],
    proposal: dict[str, dict[str, Any]],
    *,
    tail_n: int = DEFAULT_TAIL_N,
    split_pct: float = DEFAULT_SPLIT_PCT,
    min_symbols: int = MIN_SYMBOLS,
    min_tail_per_symbol: int = MIN_TAIL_PER_SYMBOL,
    min_overall_trades: int = MIN_OVERALL_TRADES,
    max_interpolated_fraction: float = MAX_INTERPOLATED_FRACTION,
) -> dict[str, Any]:
    """Run the walk-forward gate; return a verdict dict (not the bare bool).

    Top-level keys: ``verdict`` (bool), ``exit_code`` (int), ``aggregate`` (dict),
    ``per_symbol`` (dict keyed by symbol), ``insufficient_data`` (list[dict]).

    Each entry in ``insufficient_data`` looks like::

        {"symbol": "XAUUSDm",
         "reason": "interp_frac=0.83>0.5",
         "n_tail_raw": 30,
         "n_tail_evaluated": 5}
    """
    trades_all: list[dict[str, Any]] = []
    if isinstance(trade_log, dict):
        trades_all = [t for t in (trade_log.get("trades") or []) if _is_clean_trade(t)]
    elif isinstance(trade_log, list):
        trades_all = [t for t in trade_log if _is_clean_trade(t)]

    if len(trades_all) < min_overall_trades:
        return {
            "verdict": False,
            "exit_code": 2,
            "aggregate": {},
            "per_symbol": {},
            "insufficient_data": [{
                "symbol": "global",
                "reason": f"clean_trades={len(trades_all)}<{min_overall_trades}",
                "n_tail_raw": len(trades_all),
                "n_tail_evaluated": 0,
            }],
            "reason": (
                f"only {len(trades_all)} clean trades globally (< "
                f"{min_overall_trades}); need a longer history before applying patches."
            ),
        }

    # Group by symbol, take last `tail_n` per symbol chronologically.
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for t in trades_all:
        sym = t.get("symbol")
        if sym:
            by_sym.setdefault(str(sym), []).append(t)
    for sym in by_sym:
        by_sym[sym].sort(key=_trade_sort_key)
        if len(by_sym[sym]) > tail_n:
            by_sym[sym] = by_sym[sym][-tail_n:]

    insufficient_data: list[dict[str, Any]] = []
    per_symbol_results: dict[str, dict[str, Any]] = {}

    for sym, sym_trades in by_sym.items():
        _, tail = split_tail(sym_trades, tail_n=tail_n, split_pct=split_pct)
        n_tail_raw = len(tail)
        if n_tail_raw < min_tail_per_symbol:
            insufficient_data.append({
                "symbol": sym,
                "reason": f"n_tail={n_tail_raw}<{min_tail_per_symbol}",
                "n_tail_raw": n_tail_raw,
                "n_tail_evaluated": 0,
            })
            continue

        baseline_params = _resolve_baseline_params(config, live_state, sym)
        baseline_sim = _simulate_tail(tail, baseline_params)

        # Reject symbols where MAE/MFE coverage is too thin for the verdict
        # to be meaningful (interpolated rows always have delta=0 and would
        # silently inflate n_symbols_evaluated). The tail is excluded from the
        # aggregate when over ``max_interpolated_fraction`` of its rows fall
        # through.
        interp_frac = (baseline_sim["interpolated_n"] / n_tail_raw) if n_tail_raw else 0.0
        n_tail_evaluated = baseline_sim["n_tail"]
        if n_tail_raw > 0 and interp_frac > max_interpolated_fraction:
            insufficient_data.append({
                "symbol": sym,
                "reason": f"interp_frac={interp_frac:.2f}>{max_interpolated_fraction}",
                "n_tail_raw": n_tail_raw,
                "n_tail_evaluated": n_tail_evaluated,
            })
            continue

        proposed_entry = proposal.get(sym)
        if proposed_entry is not None:
            proposed_params = (
                float(proposed_entry["break_even"]["trigger_atr_mult"]),
                float(proposed_entry["break_even"]["lock_profit_atr_mult"]),
                float(proposed_entry["trailing"]["activation_atr_mult"]),
                float(proposed_entry["trailing"]["trail_atr_mult"]),
            )
        else:
            proposed_params = baseline_params  # zero-change symbol

        proposed_sim = _simulate_tail(tail, proposed_params)

        per_symbol_results[sym] = {
            "n_tail_raw": n_tail_raw,
            "n_tail": n_tail_evaluated,
            "interpolated_frac": round(interp_frac, 4),
            "baseline_net_R": round(baseline_sim["net_R"], 4),
            "baseline_expectancy_R": round(baseline_sim["expectancy_R"], 4),
            "proposed_net_R": round(proposed_sim["net_R"], 4),
            "proposed_expectancy_R": round(proposed_sim["expectancy_R"], 4),
            "net_R_delta": round(proposed_sim["net_R"] - baseline_sim["net_R"], 4),
            "expectancy_delta": round(proposed_sim["expectancy_R"] - baseline_sim["expectancy_R"], 4),
            "baseline_params": {
                "be_trig": baseline_params[0], "be_lock": baseline_params[1],
                "tr_act": baseline_params[2], "tr_dist": baseline_params[3],
            },
            "proposed_params": {
                "be_trig": proposed_params[0], "be_lock": proposed_params[1],
                "tr_act": proposed_params[2], "tr_dist": proposed_params[3],
            },
            "interpolated_n": baseline_sim["interpolated_n"],
        }

    evaluated_symbols = set(per_symbol_results.keys())
    if len(evaluated_symbols) < min_symbols:
        # Augment insufficient_data with any symbols that had no per-symbol
        # result AND with the evaluated symbols themselves (since they pass
        # per-symbol filters but the GLOBAL minimum count wasn't met).
        for sym in sorted(set(by_sym.keys()) - evaluated_symbols):
            insufficient_data.append({
                "symbol": sym,
                "reason": "no_per_symbol_result",
                "n_tail_raw": len(by_sym[sym]),
                "n_tail_evaluated": 0,
            })
        for sym in sorted(evaluated_symbols):
            insufficient_data.append({
                "symbol": sym,
                "reason": f"evaluated_count={len(evaluated_symbols)}<{min_symbols}",
                "n_tail_raw": per_symbol_results[sym]["n_tail_raw"],
                "n_tail_evaluated": per_symbol_results[sym]["n_tail"],
            })
        return {
            "verdict": False,
            "exit_code": 2,
            "aggregate": {},
            "per_symbol": per_symbol_results,
            "insufficient_data": insufficient_data,
            "reason": (
                f"only {len(evaluated_symbols)} symbols had a usable tail (>= {min_symbols} "
                f"required); insufficient global coverage."
            ),
        }

    # Aggregate gate — losses/sums across evaluated symbols.
    # expectancy is TRADE-WEIGHTED across symbols so a 100-trade winner does
    # not weigh the same as a 5-trade loser. Net_R is summed (R-units aggregate
    # fairly across varying tick sizes).
    weighted_baseline_exp_num = sum(
        s["n_tail"] * s["baseline_expectancy_R"] for s in per_symbol_results.values()
    )
    weighted_proposed_exp_num = sum(
        s["n_tail"] * s["proposed_expectancy_R"] for s in per_symbol_results.values()
    )
    total_n = max(sum(s["n_tail"] for s in per_symbol_results.values()), 1)
    agg_baseline_exp = weighted_baseline_exp_num / total_n
    agg_proposed_exp = weighted_proposed_exp_num / total_n
    expectancy_delta = agg_proposed_exp - agg_baseline_exp
    agg_net_R_delta = sum(s["net_R_delta"] for s in per_symbol_results.values())

    verdict_ok = bool(agg_net_R_delta > 0 and expectancy_delta > 0)

    # Effective minimum evaluated rows per symbol under THIS CALL'S overrides
    # (recomputed via effective_min_evaluated so the report reflects whatever
    # --min-tail / --max-interp-frac the operator actually passed).
    aggregate = {
        "n_symbols_evaluated": len(per_symbol_results),
        "n_symbols_insufficient": len(insufficient_data),
        "metric_unit": "R",
        "expectancy_aggregation": "trade_weighted",
        "effective_min_evaluated_per_symbol": effective_min_evaluated(
            min_tail_per_symbol, max_interpolated_fraction
        ),
        "baseline_total_R": round(sum(s["baseline_net_R"] for s in per_symbol_results.values()), 4),
        "proposed_total_R": round(sum(s["proposed_net_R"] for s in per_symbol_results.values()), 4),
        "net_R_delta": round(agg_net_R_delta, 4),
        "baseline_expectancy_R": round(agg_baseline_exp, 4),
        "proposed_expectancy_R": round(agg_proposed_exp, 4),
        "expectancy_delta": round(expectancy_delta, 4),
        "interpolated_trades_total": sum(s["interpolated_n"] for s in per_symbol_results.values()),
        "interp_frac_per_symbol": {
            sym: round(v["interpolated_frac"], 4)
            for sym, v in per_symbol_results.items()
        },
    }

    return {
        "verdict": verdict_ok,
        "exit_code": 0 if verdict_ok else 1,
        "metric_unit": "R",
        "model_warning": (
            "Both baseline and proposed replay use calibrate_be_trail._exit which "
            "is OPTIMISTIC for trailing (assumes the favourable peak precedes "
            "adverse trough because only MAE/MFE are recorded in trade_log). The "
            "delta is fair INSIDE-model but absolute R-units are model-dependent. "
            "Verify tuned patches against fresh out-of-sample trades before "
            "promoting any patch from observe-only to live-apply."
        ),
        "aggregate": aggregate,
        "per_symbol": per_symbol_results,
        "insufficient_data": insufficient_data,
        "reason": (
            "net_R_delta > 0 AND expectancy_delta > 0 (PASS — trade-weighted)"
            if verdict_ok else
            f"either net_R_delta ({agg_net_R_delta:+.4f}) <= 0 OR "
            f"expectancy_delta ({expectancy_delta:+.4f}) <= 0 (FAIL)"
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Walk-forward out-of-sample gate for live config patches. "
            "Replays the last 100 trades per symbol, 30% chronologically, "
            "under baseline + proposed params, asserts net_R_delta > 0 AND "
            "expectancy_delta > 0 (trade-weighted) before allowing the proposal "
            "to overwrite state/symbol_be_trail_live.json (use --apply)."
        )
    )
    ap.add_argument("--proposal", required=True, help="Path to proposal JSON (same shape as calibrate_be_trail.py output)")
    ap.add_argument("--output", required=True, help="Path to write the pass/fail JSON report")
    ap.add_argument("--apply", action="store_true",
                    help="If proposal passes the gate, merge it into state/symbol_be_trail_live.json")
    ap.add_argument("--state-out", default="symbol_be_trail_live.json",
                    help="Filename under state/ to merge on PASS (default: symbol_be_trail_live.json)")
    ap.add_argument("--tail-n", type=int, default=DEFAULT_TAIL_N, help=f"Trades per symbol (default {DEFAULT_TAIL_N})")
    ap.add_argument("--split-pct", type=float, default=DEFAULT_SPLIT_PCT,
                    help=f"Train/tail split fraction (default {DEFAULT_SPLIT_PCT} = 70/30)")
    ap.add_argument("--min-symbols", type=int, default=MIN_SYMBOLS, help=f"Min evaluated symbols (default {MIN_SYMBOLS})")
    ap.add_argument("--min-tail", type=int, default=MIN_TAIL_PER_SYMBOL,
                    help=f"Min trades in the 30-pct tail per symbol (default {MIN_TAIL_PER_SYMBOL})")
    ap.add_argument("--min-overall", type=int, default=MIN_OVERALL_TRADES,
                    help=f"Min total trades to bother running (default {MIN_OVERALL_TRADES})")
    ap.add_argument("--max-interp-frac", type=float, default=MAX_INTERPOLATED_FRACTION,
                    help=f"Max share of MAE/MFE-missing tail rows per symbol (default {MAX_INTERPOLATED_FRACTION})")
    ap.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = ap.parse_args()

    log = setup_logger("walk_forward_microtest", "walk_forward_microtest.log")
    log.info(
        "walk-forward gate: proposal=%s apply=%s tail=%d split=%.2f",
        args.proposal, args.apply, args.tail_n, args.split_pct,
    )

    # 1. Load the proposal (this exits 3 if it fails).
    try:
        proposal = _load_proposal(Path(args.proposal))
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        log.error("proposal load failed: %s", exc)
        report = {
            "verdict": False,
            "exit_code": 3,
            "reason": f"invalid proposal: {exc}",
            "checked_at": utc_now_iso(),
            "params": vars(args),
        }
        Path(args.output).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return 3

    # 2. Load trade log + config + live state.
    trade_log = read_json_state("trade_log.json", default={}) or {}
    live_state = read_json_state(args.state_out, default={"symbols": {}}) or {"symbols": {}}

    try:
        import yaml  # noqa: WPS433
        with open(args.config, "r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        log.error("config load failed: %s", exc)
        return 3

    # 3. Run the gate.
    report = gate_check(
        trade_log, config, live_state, proposal,
        tail_n=args.tail_n,
        split_pct=args.split_pct,
        min_symbols=args.min_symbols,
        min_tail_per_symbol=args.min_tail,
        min_overall_trades=args.min_overall,
        max_interpolated_fraction=args.max_interp_frac,
    )

    report["checked_at"] = utc_now_iso()
    report["params"] = {
        "proposal_path": args.proposal,
        "tail_n": args.tail_n,
        "split_pct": args.split_pct,
        "min_symbols": args.min_symbols,
        "min_tail_per_symbol": args.min_tail,
        "min_overall_trades": args.min_overall,
        "max_interpolated_fraction": args.max_interp_frac,
        "apply": args.apply,
        "state_out": args.state_out,
    }

    log.info(
        "verdict=%s exit_code=%d reason=%s",
        report["verdict"], report["exit_code"], report.get("reason"),
    )
    if report.get("aggregate"):
        log.info(
            "aggregate: n_symbols=%d net_R_delta=%.4f expectancy_delta=%.4f",
            report["aggregate"]["n_symbols_evaluated"],
            report["aggregate"]["net_R_delta"],
            report["aggregate"]["expectancy_delta"],
        )

    # 4. Persist report.
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    write_json_state(
        args.output,
        report,
    )
    if report["exit_code"] == 0:
        log.info("PASS — wrote report to %s", args.output)
    else:
        log.info("FAILED (%s) — wrote report to %s", report.get("reason"), args.output)

    # 5. Optional apply: if PASS AND --apply, merge proposal into live state.
    if args.apply and report["verdict"]:
        merged = {
            "symbols": dict(live_state.get("symbols", {}) or {}),
            "updated_at": utc_now_iso(),
            "source": "walk_forward_microtest",
            "report_path": args.output,
            "verdict": "PASS",
        }
        for sym, params in proposal.items():
            merged_entry = dict(merged["symbols"].get(sym, {}))
            merged_entry.update({
                "source": "walk_forward_microtest",
                "walk_forward_verdict": "PASS",
                "n_tail": report["per_symbol"].get(sym, {}).get("n_tail", 0),
                "walk_forward_expectancy_delta": report["per_symbol"].get(sym, {}).get("expectancy_delta", 0.0),
                **params,
                # Carry trusted=true so position_manager._merge_live() honours it.
                "trusted": True,
            })
            merged["symbols"][sym] = merged_entry
        write_json_state(args.state_out, merged)
        log.info("--apply: merged proposal into %s (trusted=True)", args.state_out)

    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
