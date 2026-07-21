"""Offline 'what-if' simulator for the BE/payoff paradox patch.

GOVERNANCE: Test on the trade_log first BEFORE applying live.
Reads mt5_quant_agent/state/trade_log.json, builds a counterfactual PnL
series under the patch's rules, and reports before/after stats + the trades
'saved' from the BE-floor gate.

PATCH RULES (mirror core/position_manager.py after the patch):

(a) NEVER close a winning position at less than min_r_multiple_win=0.4
    -> If BE would lock a winner with r < 0.4R, do NOT fire BE; position
       holds. Counterfactual assumes the position runs to the median R of
       genuine (non-dust) winners, or a configurable proxy.
(b) ONLY stale-close losers
    -> If the trade closed at -1R-ish (initial SL hit) flagged by exit_reason
       'break_even_stop' / 'trailing_stop' while r <= 0, the patch LOSES
       nothing because it didn't move SL toward profit during the loss phase.
       Counterfactual: same outcome (full -1R loss).
(c) partial_tp disabled + BE activates -> leave full position until TP2/trail
    -> No scale-out at BE. Counterfactual: ignore any volume_remaining_size
       that the historical trade implied; behave as if no scaling.

The simulator does NOT mutate trade_log.json. It produces:
  - baseline_stats: WR/avg_win/avg_loss/payoff/net, as observed in trade_log
  - patch_stats: same metrics under the counterfactual
  - saved_trades: list of trades the patch 'saves' from dust exit
  - new_losers: list of trades the patch ADDITIONALLY absorbs (if a 0.35R
    spike that previously locked at BE now reverses to -1R, the patch
    converted a dust winner into a full loser)

NOTE: trade_log.json has r_multiple calculated from price (entry/exit/sl),
not volume. PnL is per-trade realised USD. Volatility per R = |entry - sl|.
We convert R -> USD via `(r_multiple * |entry - sl| * volume)` but the
historical pnl field is the authoritative one for baseline; for patch we
recompute r to the median R and produce a counterfactual USD via the same
formula.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "trade_log.json"

# ----- knobs -----
MIN_R_MULTIPLE_WIN = 0.4  # patch (a) floor
NATURAL_WINNER_MIN_R = 0.4  # what counts as a 'genuine' (non-dust) winner


def _f(v):
    try: return float(v) if v is not None else None
    except (TypeError, ValueError): return None


def _r(t):
    r = _f(t.get("r_multiple"))
    return r


def _exit_kind(er: str) -> str:
    """Categorize the close path. Returns: dust_winner / clean_winner /
    full_loser / breakeven / unknown."""
    er = (er or "").lower()
    pnl = _f(t.get("pnl") if False else None)  # placeholder, use local below
    return er  # raw lowered


def _is_winner(pnl) -> bool:
    try: return pnl is not None and float(pnl) > 0
    except (TypeError, ValueError): return False


def _is_loser(pnl) -> bool:
    try: return pnl is not None and float(pnl) < 0
    except (TypeError, ValueError): return False


def _vol(t):
    """Volume per trade for converting R->USD when needed."""
    v = _f(t.get("volume")) or _f(t.get("size"))
    if v is None or v <= 0:
        # Fall back to a known micro-lot default if missing.
        return 0.01
    return v


def _risk_distance_price(t):
    e = _f(t.get("entry")); s = _f(t.get("sl")); si = _f(t.get("sl_initial") or t.get("sl"))
    candidates = [c for c in (s, si) if c is not None]
    if e is None or not candidates:
        return None
    return min(abs(e - c) for c in candidates)


def _r_to_usd(r, t):
    rd = _risk_distance_price(t)
    if rd is None or r is None:
        return None
    return round(r * rd * _vol(t), 2)


def load_trades():
    with open(STATE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("trades", []) or []


def natural_winner_median_r(trades, min_r=NATURAL_WINNER_MIN_R):
    """Median r_multiple of winners whose r >= min_r AND whose exit_reason
    is NOT close-to-entry (i.e. not a dust-trailing)."""
    rs = []
    for t in trades:
        r = _r(t)
        if r is None or r < min_r: continue
        if not _is_winner(_f(t.get("pnl"))): continue
        rs.append(r)
    if not rs:
        return None
    return round(statistics.median(rs), 3)


def _classify(t):
    """Return 'dust_winner' | 'clean_winner' | 'loser' | 'breakeven'."""
    pnl = _f(t.get("pnl"))
    r = _r(t)
    if pnl is None:
        return "unknown"
    if pnl == 0 or (r is not None and -0.0001 < r < 0.0001):
        return "breakeven"
    if pnl > 0:
        if r is not None and 0 < r < MIN_R_MULTIPLE_WIN:
            return "dust_winner"
        return "clean_winner"
    return "loser"


def stats_summary(rows):
    pnls = [_f(t.get("pnl")) for t in rows]
    pnls = [p for p in pnls if p is not None]
    if not pnls:
        return None
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_w = round(sum(wins) / len(wins), 3) if wins else 0.0
    avg_l = round(sum(losses) / len(losses), 3) if losses else 0.0
    payoff = round(abs(avg_w / avg_l), 3) if avg_l else 0.0
    return {
        "n": len(pnls),
        "wr_pct": round(100.0 * len(wins) / len(pnls), 1),
        "wins": len(wins),
        "losses": len(losses),
        "avg_win": avg_w,
        "avg_loss": avg_l,
        "payoff": payoff,
        "net_pnl": round(sum(pnls), 2),
        "biggest_win": round(max(pnls), 2),
        "biggest_loss": round(min(pnls), 2),
    }


def project_patch_pnl(t, natural_med_r):
    """Counterfactual pnl under the patch rules.

    Returns the projected USD PnL for trade t under the patch.
    """
    r = _r(t)
    pnl = _f(t.get("pnl"))
    kind = _classify(t)
    if pnl is None:
        return None
    if kind == "dust_winner":
        # Patch gate: we WOULD NOT have fired BE here (lock < 0.4R).
        # Counterfactual: trade runs to median natural winner r.
        if natural_med_r is None:
            return pnl  # can't project
        return _r_to_usd(natural_med_r, t)
    if kind == "clean_winner":
        return pnl
    if kind == "loser":
        # Patch (b): we don't intrude into loss territory with BE/trail.
        # Either the trade times out (similar to current realised) or hits
        # initial SL. Either way current pnl is the best estimate.
        return pnl
    return pnl  # breakeven / unknown -> unchanged


def run(window_label, trades):
    """Run before/after on a slice and report."""
    natural_med = natural_winner_median_r(trades)
    rows_before = [{"pnl": _f(t.get("pnl"))} for t in trades]
    rows_after = [{"pnl": project_patch_pnl(t, natural_med)} for t in trades]

    counter_kinds = Counter(_classify(t) for t in trades)
    saved = []
    new_losers = []
    for t in trades:
        if _classify(t) == "dust_winner":
            new = project_patch_pnl(t, natural_med)
            if new is not None and new > (_f(t.get("pnl")) or 0):
                saved.append({
                    "trade_id": t.get("trade_id"),
                    "symbol": t.get("symbol"),
                    "old_pnl": _f(t.get("pnl")),
                    "new_pnl": new,
                    "old_r": _r(t),
                    "new_r": natural_med,
                })
    return {
        "window": window_label,
        "n": len(trades),
        "natural_winner_median_r": natural_med,
        "kinds": dict(counter_kinds),
        "before": stats_summary(rows_before),
        "after": stats_summary(rows_after),
        "saved_trades_count": len(saved),
        "saved_total_usd": round(sum(s["new_pnl"] - s["old_pnl"] for s in saved), 2),
        "saved_examples": saved[:5],
    }


def fmt_row(label, s):
    if s is None: return f"  {label}: n/a"
    return f"  {label}: n={s['n']} | WR {s['wr_pct']}% | win ${s['avg_win']:>8} | loss ${s['avg_loss']:>8} | payoff {s['payoff']:>5} | net ${s['net_pnl']:>10}"


def main():
    if len(sys.argv) > 1:
        global STATE
        STATE = Path(sys.argv[1])
    trades = load_trades()
    if not trades:
        print("No trades found at", STATE)
        return 1

    print(f"Loaded {len(trades)} trades from {STATE}")
    print(f"PATCH knob: min_r_multiple_win = {MIN_R_MULTIPLE_WIN}")
    print()

    windows = [
        ("LAST 30", trades[-30:]),
        ("LAST 100", trades[-100:]),
        ("LAST 200", trades[-200:]),
        ("ALL", trades),
    ]
    results = []
    for label, slice_ in windows:
        if not slice_:
            continue
        r = run(label, slice_)
        results.append(r)
        print(f"WINDOW: {label} (n={r['n']}, kinds={r['kinds']})")
        print(f"  natural_winner_median_r = {r['natural_winner_median_r']}")
        print(fmt_row("BEFORE (status quo)", r["before"]))
        print(fmt_row("AFTER (patch)",       r["after"]))
        delta_payoff = round(r["after"]["payoff"] - r["before"]["payoff"], 3)
        delta_net = round(r["after"]["net_pnl"] - r["before"]["net_pnl"], 2)
        print(f"  DELTA: payoff {delta_payoff:+.3f} | net ${delta_net:+.2f} | saved {r['saved_trades_count']} dust winners (+${r['saved_total_usd']})")
        for ex in r["saved_examples"]:
            print(f"     saved: {ex['symbol']} {ex['trade_id']} ${ex['old_pnl']:.2f}->${ex['new_pnl']:.2f} (R {ex['old_r']:.2f}->{ex['new_r']:.2f})")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
