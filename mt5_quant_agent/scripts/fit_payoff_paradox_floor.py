"""One-shot fitter for the Payoff Paradox BE-floor (2026-07-20).

Reads state/payoff_paradox_audit.jsonl, derives per-symbol median winner R,
recommends a floor in candidate_floors = {0.4, 0.5, 0.6}, applies the BTC
auto-promote rule, writes the verdict to state/payoff_paradox_fit.json, and
optionally --write the recommendation back into config.yaml.

DESIGN
* Pure read at scan time — no mutation of trade_log, audit, or state except
  the fit summary file (and config.yaml when --write is on).
* Idempotent — running twice yields the same JSON (modulo timestamp) so the
  operator can cron it every N closed trades.
* Self-tuning rule:
    per_symbol, median winner r ->
      median_r > 0.65 -> recommend 0.6
      median_r > 0.45 -> recommend 0.5
      otherwise        -> recommend 0.4 (the floor)
* BTC auto-promote rule (applied AFTER per-symbol fit):
    symbol 'BTCUSDm':
      if (n_last_30 >= btc_min_n) AND (median(winner_r of last 30) > btc_median_threshold)
      AND IQR/median < 0.5 (stability):
        promote_floor -> recommended globally regardless of per-symbol fit
* ``--write`` writes the GLOBAL recommended floor back to
  ``trading.exits.min_r_multiple_win`` in config.yaml — operator-driven
  oversight, never automatic in bot cycles.

USAGE
    python scripts/fit_payoff_paradox_floor.py
    python scripts/fit_payoff_paradox_floor.py --dry-run
    python scripts/fit_payoff_paradox_floor.py --write
    python scripts/fit_payoff_paradox_floor.py --last-n 30  # only fit on last-N trades
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402  (PyYAML via requirements)

from core.utils import (  # noqa: E402
    STATE_DIR,
    read_payoff_paradox_audit_cached,
    utc_now_iso,
)


DEFAULT_FIT_FILENAME = "payoff_paradox_fit.json"
DEFAULT_AUDIT_FILENAME = "payoff_paradox_audit.jsonl"
DEFAULT_TRADE_LOG_FILENAME = "trade_log.json"
DEFAULT_CONFIG_FILENAME = "config.yaml"
DEFAULT_CANDIDATE_FLOORS: tuple[float, ...] = (0.4, 0.5, 0.6)


def _safe_load_yaml_config(filename: str) -> dict[str, Any]:
    """Load config.yaml via yaml.safe_load (config.yaml is YAML, not JSON —
    read_json_state silently returns the default because yaml fails the json
    parser, hiding every operator-customised knob). Falls back to {} on
    FileNotFoundError so tests with a tmp_path fixture still work.
    """
    path = PROJECT_ROOT / filename
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return {}


def _projected_floor_from_cfg(cfg: dict[str, Any]) -> float:
    try:
        v = cfg["trading"]["exits"]["min_r_multiple_win"]
        return float(v)
    except (KeyError, TypeError, ValueError):
        return 0.4


def _self_tune_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return (
        cfg.get("trading", {})
        .get("exits", {})
        .get("payoff_paradox", {})
        .get("self_tune", {})
        or {}
    )


def _btc_promote_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return (
        cfg.get("trading", {})
        .get("exits", {})
        .get("payoff_paradox", {})
        .get("btc_promote", {})
        or {}
    )


def floor_for_median_r(median_r: float, candidates: tuple[float, ...] = DEFAULT_CANDIDATE_FLOORS) -> float:
    """Bucket a winner-median-R into one of the candidate floors.

    The bucketing rule (intentionally simple and auditable):

      median_r > 0.65 -> 0.6   (typical gold winner at 1.5R tp1, ratchets up fast)
      median_r > 0.45 -> 0.5   (typical FX winner — sweet spot, leaves 0.05R on table)
      otherwise        -> 0.4   (default starting floor)

    The rule is symmetric around 0.55R (the median of 0.45 and 0.65). The
    0.20R buffer above the chosen floor lets winners hover before being
    locked at the floor level.
    """
    if not isinstance(median_r, (int, float)) or median_r <= 0:
        return candidates[0]
    if median_r > 0.65:
        return 0.6
    if median_r > 0.45:
        return 0.5
    return 0.4


def fit_per_symbol(
    rows: list[dict[str, Any]],
    current_floor: float,
    min_per_symbol_winners: int,
    min_median_winner_r: float,
) -> list[dict[str, Any]]:
    """Build per-symbol recommendation table. Returns a sorted list of dicts."""
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        by_symbol.setdefault(str(sym), []).append(r)

    out: list[dict[str, Any]] = []
    for sym, recs in sorted(by_symbol.items()):
        n_closed = len(recs)
        # REVIEW FIX 3: unpriced SL rows are marked r=None by the row builder;
        # filter to numeric r>0 winners only so the median isn't biased by
        # the all-r=0 unpriced cohort.
        winners = []
        for r in recs:
            rval = r.get("r")
            if isinstance(rval, (int, float)) and float(rval) > 0:
                winners.append(float(rval))
        n_winners = len(winners)
        median_winner_r = statistics.median(winners) if winners else 0.0
        q1, q3 = (0.0, 0.0)
        iqr = 0.0
        if len(winners) >= 4:
            try:
                # quantiles with n=4 returns [Q1, Q2, Q3] — 3 cut points
                _q1, _q2, _q3 = statistics.quantiles(winners, n=4)
                q1, q3 = float(_q1), float(_q3)
                iqr = q3 - q1
            except statistics.StatisticsError:
                q1, q3 = (0.0, 0.0)
                iqr = 0.0
        if n_winners < min_per_symbol_winners:
            recommended = current_floor
            reason = (
                f"insufficient_winners (n_winners={n_winners} < "
                f"{min_per_symbol_winners}); hold current floor"
            )
        elif median_winner_r < min_median_winner_r:
            recommended = current_floor
            reason = (
                f"median_winner_r {median_winner_r:.3f} < "
                f"min_median_winner_r {min_median_winner_r:.3f}; hold current floor"
            )
        else:
            recommended = floor_for_median_r(median_winner_r, DEFAULT_CANDIDATE_FLOORS)
            reason = (
                f"median_winner_r {median_winner_r:.3f} -> floor {recommended} "
                f"(n_closed={n_closed}, n_winners={n_winners}, iqr={iqr:.3f})"
            )
        out.append({
            "symbol": sym,
            "n_closed": n_closed,
            "n_winners": n_winners,
            "median_winner_r": round(median_winner_r, 6),
            "q1_winner_r": round(q1, 6),
            "q3_winner_r": round(q3, 6),
            "iqr_winner_r": round(iqr, 6),
            "current_floor": round(current_floor, 4),
            "recommended_floor": round(float(recommended), 4),
            "reason": reason,
        })
    return out


def fit_btc_promote(
    rows: list[dict[str, Any]],
    current_floor: float,
    btc_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Apply the BTC auto-promote rule. Returns a verdict dict.

    Rule (per the user spec):
      eligible := (BTC symbol has >= btc_min_n CLOSED trades among the
                   LAST btc_min_n of its rows) AND
                  (winner-median-r across those last-n > btc_median_threshold) AND
                  (stable := IQR / median_winner_r < 0.5)
      promote := btc_promote.promote_floor (typically 0.5)
    """
    symbol = btc_cfg.get("symbol") or "BTCUSDm"
    try:
        btc_min_n = int(btc_cfg.get("min_n", 30))
    except (TypeError, ValueError):
        btc_min_n = 30
    try:
        threshold = float(btc_cfg.get("median_threshold", 0.5))
    except (TypeError, ValueError):
        threshold = 0.5
    promote_floor = float(btc_cfg.get("promote_floor", 0.5))

    sym_rows = [r for r in rows if str(r.get("symbol") or "") == symbol]
    winners = [float(r.get("r", 0)) for r in sym_rows
               if isinstance(r.get("r"), (int, float)) and float(r.get("r", 0)) > 0]
    last_n_winners = winners[-btc_min_n:] if len(winners) >= btc_min_n else winners
    n_last = len(last_n_winners)
    median_r = statistics.median(last_n_winners) if last_n_winners else 0.0

    # Stability: IQR/median. Skip the check when n<4 (quantiles raises).
    # statistics.quantiles(data, n=4) returns 3 cut points [Q1, Q2, Q3].
    iqr_over_median = None
    if n_last >= 4:
        try:
            _q1, _q2, _q3 = statistics.quantiles(last_n_winners, n=4)
            if median_r > 0:
                iqr_over_median = round((_q3 - _q1) / median_r, 6)
        except statistics.StatisticsError:
            iqr_over_median = None

    eligible = n_last >= btc_min_n and median_r > threshold
    if iqr_over_median is not None:
        # Belt and braces: don't auto-promote on high median-R but massive variance.
        eligible = eligible and iqr_over_median < 0.5
    return {
        "enabled": bool(btc_cfg.get("enabled", True)),
        "symbol": symbol,
        "min_n": btc_min_n,
        "median_threshold": threshold,
        "promote_floor": round(promote_floor, 4),
        "n_last_n_winners": n_last,
        "median_winner_r_last_n": round(median_r, 6),
        "iqr_over_median": iqr_over_median,
        "eligible": bool(eligible),
        "recommend_promote": bool(eligible),
        "current_floor": round(current_floor, 4),
        "reason": (
            f"BTC last {btc_min_n}: median={median_r:.3f} > threshold={threshold:.3f}; "
            f"n_winners={n_last}; iqr/median={iqr_over_median}"
            if eligible else
            f"BTC not eligible: median={median_r:.3f} (need >{threshold:.3f}); n={n_last}"
        ),
    }


def infer_global_floor(
    per_symbol: list[dict[str, Any]],
    btc_verdict: dict[str, Any],
    current_floor: float,
    candidates: tuple[float, ...] = DEFAULT_CANDIDATE_FLOORS,
) -> tuple[float, str]:
    """Pick one GLOBAL floor — the median of per-symbol recs clamped to the
    candidate set. BTC promote wins over per-symbol if approved."""
    if btc_verdict.get("recommend_promote"):
        floor = float(btc_verdict.get("promote_floor") or candidates[0])
        return floor, (
            f"BTC_AUTO_PROMOTE median={btc_verdict.get('median_winner_r_last_n'):.3f} "
            f"> threshold={btc_verdict.get('median_threshold'):.3f}"
        )
    recs = [r["recommended_floor"] for r in per_symbol
            if r["recommended_floor"] > r["current_floor"]]
    if not recs:
        return current_floor, "no_symbol_steps_up: hold current floor"
    median_pick = statistics.median(recs)
    # REVIEW FIX (precision-safe tie-breaker): abs() returns are float-comparison
    # so |0.5-0.55| may compute slightly larger or smaller than |0.6-0.55|
    # depending on the platform's IEEE-754 path. Round to 9 decimals BEFORE
    # the min comparison so the tie-breaker is deterministic; then prefer the
    # FIRST candidate in iteration order so the operator can predict the
    # chosen floor from the candidate_floors list.
    nearest = min(
        candidates,
        key=lambda c: (round(abs(float(c) - median_pick), 9), candidates.index(c)),
    )
    # Only step UP, never down (a floor decrease is a regression — leave it).
    nearest = max(float(nearest), current_floor)
    return float(nearest), f"per_symbol_median_recommendation={median_pick:.3f} -> snapped to {nearest}"


def _writeback_to_config(write_path: Path, new_floor: float) -> bool:
    """Surgical config.yaml writeback — preserve rest of file structure,
    only mutate /trading/exits/min_r_multiple_win.

    Returns True on success. The file is read as raw text to keep comments,
    ordering, CRLF, and the surrounding structure intact.
    """
    try:
        raw = write_path.read_text(encoding="utf-8")
    except OSError:
        return False
    needle = "min_r_multiple_win:"
    idx = raw.find(needle)
    if idx < 0:
        return False
    start = idx + len(needle)
    end = raw.find("\n", start)
    if end < 0:
        end = len(raw)
    before = raw[:start]
    suffix = raw[end:]
    new_value = f" {new_floor}"
    new_raw = before + new_value + suffix
    backup = write_path.with_suffix(
        write_path.suffix + f".prewritebac_{int(time.time() * 1e6)}"
    )
    try:
        shutil.copy2(write_path, backup)
    except OSError:
        pass
    tmp = write_path.with_suffix(write_path.suffix + ".tmp")
    try:
        tmp.write_text(new_raw, encoding="utf-8")
        os.replace(tmp, write_path)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _fit_blob_path() -> Path:
    return STATE_DIR / DEFAULT_FIT_FILENAME


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and print; do not write the fit file.")
    parser.add_argument("--write", action="store_true",
                        help="Also write the GLOBAL recommended floor back into config.yaml.")
    parser.add_argument("--last-n", type=int, default=None,
                        help="Only fit on the last N trades (default: all).")
    parser.add_argument("--audit-filename", default=DEFAULT_AUDIT_FILENAME,
                        help=f"Audit JSONL filename (default: {DEFAULT_AUDIT_FILENAME}).")
    parser.add_argument("--fit-filename", default=DEFAULT_FIT_FILENAME,
                        help=f"Output fit JSON filename (default: {DEFAULT_FIT_FILENAME}).")
    parser.add_argument("--source-trade-log", default=DEFAULT_TRADE_LOG_FILENAME,
                        help=f"trade_log.json filename (default: {DEFAULT_TRADE_LOG_FILENAME}).")
    args = parser.parse_args()

    cfg = _safe_load_yaml_config(DEFAULT_CONFIG_FILENAME)
    current_floor = _projected_floor_from_cfg(cfg)
    st = _self_tune_cfg(cfg)
    try:
        min_per_winners = int(st.get("min_per_symbol_winners", 10))
    except (TypeError, ValueError):
        min_per_winners = 10
    try:
        min_median_r = float(st.get("min_median_winner_r", 0.15))
    except (TypeError, ValueError):
        min_median_r = 0.15
    try:
        min_n_fit = int(st.get("min_n_closed_for_fit", 30))
    except (TypeError, ValueError):
        min_n_fit = 30
    try:
        candidates: tuple[float, ...] = tuple(
            float(x) for x in (st.get("candidate_floors") or DEFAULT_CANDIDATE_FLOORS)
        )
    except (TypeError, ValueError):
        candidates = DEFAULT_CANDIDATE_FLOORS

    rows = read_payoff_paradox_audit_cached(args.audit_filename)
    print(f"[fit] audit rows loaded : {len(rows)} (from {args.audit_filename})")
    if args.last_n is not None and args.last_n > 0:
        rows = rows[-args.last_n:]
        print(f"[fit] trimmed to last_n : {len(rows)}")

    verdict = "insufficient_data"
    if len(rows) < min_n_fit:
        verdict = f"insufficient_data ({len(rows)}<{min_n_fit} closed)"
        per_symbol: list[dict[str, Any]] = []
        btc_verdict: dict[str, Any] = fit_btc_promote(rows, current_floor, _btc_promote_cfg(cfg))
        global_floor = current_floor
        reason = verdict
    else:
        per_symbol = fit_per_symbol(rows, current_floor, min_per_winners, min_median_r)
        btc_verdict = fit_btc_promote(rows, current_floor, _btc_promote_cfg(cfg))
        global_floor, reason = infer_global_floor(per_symbol, btc_verdict, current_floor, candidates)
        if abs(global_floor - current_floor) < 1e-12:
            verdict = "no_change_recommended"
        elif btc_verdict.get("recommend_promote"):
            verdict = "btc_auto_promote"
        else:
            verdict = "step_up_recommended"

    blob = {
        "timestamp": utc_now_iso(),
        "verdict": verdict,
        "reason": reason,
        "current_floor": round(current_floor, 4),
        "recommended_floor": round(global_floor, 4),
        "candidate_floors": list(candidates),
        "audit_rows_used": len(rows),
        "thresholds": {
            "min_n_closed_for_fit": min_n_fit,
            "min_per_symbol_winners": min_per_winners,
            "min_median_winner_r": min_median_r,
        },
        "per_symbol": per_symbol,
        "btc_promote": btc_verdict,
        "config_writeback": {
            "attempted": bool(args.write),
            "success": False,
            "old_floor": round(current_floor, 4),
            "new_floor": round(global_floor, 4),
        },
    }

    if args.write and not args.dry_run and verdict != "insufficient_data" and abs(global_floor - current_floor) > 1e-12:
        ok = _writeback_to_config(PROJECT_ROOT / DEFAULT_CONFIG_FILENAME, global_floor)
        blob["config_writeback"]["success"] = ok
        blob["config_writeback"]["config_path"] = str(PROJECT_ROOT / DEFAULT_CONFIG_FILENAME)
        if ok:
            print(f"[fit] config.yaml updated: min_r_multiple_win -> {global_floor}")
        else:
            print(f"[fit] config.yaml writeback FAILED — kept {current_floor}")

    fit_path = _fit_blob_path() if not args.dry_run else None
    if args.dry_run:
        print()
        print(json.dumps(blob, indent=2))
    else:
        if fit_path is None:
            fit_path = STATE_DIR / args.fit_filename
        tmp = fit_path.with_suffix(fit_path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(blob, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, fit_path)
            print(f"[fit] wrote {fit_path} ({len(json.dumps(blob))} bytes)")
        except OSError as exc:
            print(f"[fit] write FAILED: {exc}")
            return 2

    print()
    print(f"[fit] verdict            : {verdict}")
    print(f"[fit] reason             : {reason}")
    print(f"[fit] current floor      : {current_floor}")
    print(f"[fit] REcommended floor  : {global_floor}")
    print(f"[fit] btc eligible       : {btc_verdict.get('eligible')}")
    if btc_verdict.get("eligible"):
        print(f"[fit] btc last-N median  : {btc_verdict.get('median_winner_r_last_n')}")
        print(f"[fit] btc last-N IQR/med : {btc_verdict.get('iqr_over_median')}")
    print(f"[fit] per_symbol recs    : {len(per_symbol)}")
    for r in per_symbol[:5]:
        print(
            f"   - {r['symbol']:<14} n={r['n_closed']:>4} wins={r['n_winners']:>4} "
            f"median_r={r['median_winner_r']:>7.3f} | "
            f"{r['current_floor']} -> {r['recommended_floor']} ({r['reason']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
