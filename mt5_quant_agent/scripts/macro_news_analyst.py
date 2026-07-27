"""Macro / environmental-factor analyst (cycle 18, 6th edge family).

User request: "a new analyst for predictive news trading from macro
environmental factors, test every hour on past data looking for correlations
for edge."

This is a FACTOR-RESEARCH scan, not a parameter optimisation. At each hour t it
computes macro/environmental factors from data available strictly up to t (no
look-ahead), then asks whether any factor bucket predicts the forward 4h XAUUSDm
return net of retail cost. The honest multiple-testing gates (DSR / Hansen SPA /
CSCV PBO) already used across the project decide whether any bucket has a real
OOS edge.

Honesty notes (read before trusting a positive):
- **Close-to-close, fixed 4h hold.** No intrabar SL/TP. Per the exit-model-bias
  finding, close-to-close is the OPTIMISTIC bound: if a factor fails here it is
  cleanly dead; if it passes here it still needs intrabar confirmation before any
  deploy talk.
- **Event windows use a CONSTRUCTED calendar** (first-Friday NFP, hardcoded FOMC
  dates, 2nd Tue/Wed CPI proxy) -- an approximation of the real economic
  calendar, not a feed. N is small (~12-36/yr) so event-only buckets cannot clear
  DSR alone; they are reported as a correlation sanity check, not a deploy path.
- **No external macro data** (no FRED / calendar API). Cross-asset factors are
  derived from the staged XAU/USOIL/BTC M5 themselves (oil + btc 1h return as
  risk-appetite / commodity proxies). This is a venue-internal macro proxy.
- Cost: 30 bps round-trip (retail XAU). Per the project convention.

NO LIVE TRADING. Read-only backtest on staged parquet history.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist, mean, variance

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.history_manager import HISTORY_DIR
from scripts.quantum_loop import deflated_sharpe
from scripts.validation_audit import cscv_pbo, spa_pvalue

COST_RT = 0.0030          # 30 bps round-trip, retail XAU/USOIL
HOLD_HOURS = 4            # forward-hold window
N_MIN_IS = 20             # min train samples for a bucket to be signal-eligible
N_MIN_OOS = 30            # min OOS trades for a bucket to be verdict-eligible
COST_THRESH = 0.0008      # 8 bps -- |IS conditional mean| must clear this + cost

# Approximate FOMC announcement dates (UTC, 18:00 ~= 2pm press conference window).
# Public schedule; treated as a constructed calendar, not a live feed.
FOMC_DATES = [
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31",
    "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30",
    "2025-09-17", "2025-10-29", "2025-12-17",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
]


def _load_h1(symbol: str) -> pd.DataFrame:
    """Load staged M5 parquet, resample to H1 OHLC."""
    df = pd.read_parquet(HISTORY_DIR / f"{symbol}_M5.parquet")
    if "time" not in df.columns:
        df = df.rename(columns={df.columns[0]: "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    ohlc_map = {}
    for col in ("open", "high", "low", "close", "Open", "High", "Low", "Close"):
        if col in df.columns:
            ohlc_map[col] = col.lower()
    df = df[list(ohlc_map.keys())].rename(columns=ohlc_map)
    h1 = df.resample("1h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    return h1


def _session(hour: int) -> str:
    if 23 <= hour or hour < 7:
        return "asia"
    if 7 <= hour < 13:
        return "london"
    if 13 <= hour < 18:
        return "ny"
    return "off"


def _vol_regime(atr_p: float) -> str:
    if atr_p < 0.333:
        return "low"
    if atr_p < 0.667:
        return "mid"
    return "high"


def _is_first_friday(dt) -> bool:
    return dt.weekday() == 4 and dt.day <= 7


def _is_cpi_day(dt) -> bool:
    # US CPI: typically 2nd Tue or Wed of the month, 12:30 UTC. Approximate.
    if dt.weekday() not in (1, 2):  # Tue=1, Wed=2
        return False
    day = dt.day
    # 2nd Tue/Wed: day in 8..14 roughly
    return 8 <= day <= 14


def _event_flags(dt) -> tuple[int, int, int]:
    """(nfp, fomc, cpi) flags for whether dt is within +/-1h of a release."""
    nfp = 1 if (_is_first_friday(dt) and 11 <= dt.hour <= 14) else 0
    fomc = 0
    for d in FOMC_DATES:
        if dt.strftime("%Y-%m-%d") == d and 17 <= dt.hour <= 20:
            fomc = 1
            break
    cpi = 1 if (_is_cpi_day(dt) and 11 <= dt.hour <= 14) else 0
    return nfp, fomc, cpi


def _features(h1: pd.DataFrame, oil_h1: pd.Series, btc_h1: pd.Series) -> pd.DataFrame:
    """Build the factor frame indexed by hour, strictly no look-ahead."""
    f = pd.DataFrame(index=h1.index)
    f["close"] = h1["close"]
    f["hour"] = h1.index.hour
    f["dow"] = h1.index.dayofweek
    f["session"] = f["hour"].apply(_session)
    # rolling 24h ATR percentile (prior 24h, no look-ahead)
    tr = (h1["high"] - h1["low"]).rolling(24, min_periods=12).mean()
    atr_p = tr.rolling(252, min_periods=60).rank(pct=True)
    f["vol_regime"] = atr_p.apply(lambda x: _vol_regime(x) if pd.notna(x) else "na")
    # EMA50 slope sign (prior bar)
    ema50 = h1["close"].ewm(span=50, min_periods=50).mean()
    slope = ema50.diff()
    f["trend"] = slope.apply(lambda x: "up" if x > 0 else ("down" if x < 0 else "flat"))
    # cross-asset 1h return signs (prior hour, known at t)
    f["oil_1h"] = oil_h1.shift(1).reindex(f.index).apply(lambda x: "up" if x > 0 else ("down" if x < 0 else "flat"))
    f["btc_1h"] = btc_h1.shift(1).reindex(f.index).apply(lambda x: "up" if x > 0 else ("down" if x < 0 else "flat"))
    # forward 4h return (the thing we predict)
    f["fwd_ret"] = h1["close"].shift(-HOLD_HOURS) / h1["close"] - 1.0
    # event flags
    ev = f.index.to_series().apply(_event_flags)
    f["nfp"] = ev.apply(lambda t: t[0])
    f["fomc"] = ev.apply(lambda t: t[1])
    f["cpi"] = ev.apply(lambda t: t[2])
    return f.dropna(subset=["fwd_ret"])


def _ci95_lo(rs: list[float]) -> float:
    n = len(rs)
    if n < 2:
        return 0.0
    m = mean(rs); v = variance(rs, m)
    return m - NormalDist().inv_cdf(0.975) * math.sqrt(v / n)


def _folds(idx: pd.DatetimeIndex, k: int) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    start, end = idx.min(), idx.max()
    span = (end - start).total_seconds()
    out = []
    for i in range(k):
        t_end = start + pd.Timedelta(seconds=span * (i + 1) / (k + 1))
        te_end = start + pd.Timedelta(seconds=span * (i + 2) / (k + 1)) if i < k - 1 else end
        out.append((start, t_end, t_end, te_end))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Macro/news environmental-factor analyst")
    ap.add_argument("--target", default="XAUUSDm", help="asset to trade")
    ap.add_argument("--oil", default="USOILm")
    ap.add_argument("--btc", default="BTCUSDm")
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--cost-rt", type=float, default=COST_RT)
    ap.add_argument("--out", type=Path, default=ROOT / "macro_news_report.json")
    args = ap.parse_args()

    print(f"Macro/news analyst: target={args.target} oil={args.oil} btc={args.btc} "
          f"hold={HOLD_HOURS}h cost={args.cost_rt*1e4:.0f}bps RT folds={args.folds}")

    tgt = _load_h1(args.target)
    oil = _load_h1(args.oil)["close"].pct_change()
    btc = _load_h1(args.btc)["close"].pct_change()
    feat = _features(tgt, oil, btc)
    print(f"H1 feature rows: {len(feat)}  window {feat.index.min()} -> {feat.index.max()}")

    FACTOR_KEYS = ["session", "vol_regime", "trend", "oil_1h", "btc_1h"]

    # ---- per-bucket IS learning + OOS trading, walk-forward ----
    folds = _folds(feat.index, args.folds)
    # bucket -> list of OOS net returns across all folds
    bucket_oos: dict[tuple, list[float]] = defaultdict(list)
    # also track per-fold positivity
    bucket_fold_pos: dict[tuple, list[bool]] = defaultdict(list)
    # IS scan size (number of distinct buckets considered) for DSR deflation
    all_buckets: set[tuple] = set()

    for fi, (tr_s, tr_e, te_s, te_e) in enumerate(folds):
        train = feat[(feat.index >= tr_s) & (feat.index < tr_e)]
        test = feat[(feat.index >= te_s) & (feat.index < te_e)]
        # IS conditional means per bucket
        is_signals: dict[tuple, int] = {}  # bucket -> direction (+1/-1)
        grouped = train.groupby(FACTOR_KEYS)
        for key, g in grouped:
            all_buckets.add(key)
            if len(g) < N_MIN_IS:
                continue
            mu = g["fwd_ret"].mean()
            if abs(mu) >= COST_THRESH + args.cost_rt:
                is_signals[key] = 1 if mu > 0 else -1
        # OOS: trade each test hour whose bucket is a signal
        test_grouped = test.groupby(FACTOR_KEYS)
        for key, g in test_grouped:
            if key in is_signals:
                d = is_signals[key]
                net = (d * g["fwd_ret"] - args.cost_rt).tolist()
                bucket_oos[key].extend(net)
                bucket_fold_pos[key].append(mean(net) > 0 if net else False)

    # ---- per-bucket stats ----
    buckets = [k for k, v in bucket_oos.items() if len(v) >= N_MIN_OOS]
    # sr_var across all scanned buckets (IS Sharpe variance for deflation)
    # approximate with OOS-bucket Sharpe variance; fallback 0
    def _sharpe(rs):
        if len(rs) < 2:
            return 0.0
        m = mean(rs); sd = math.sqrt(variance(rs, m))
        return m / sd if sd > 0 else 0.0
    sr_var = variance([_sharpe(bucket_oos[k]) for k in buckets]) if len(buckets) > 1 else 0.0
    n_trials = max(len(all_buckets), len(buckets), 1)

    rows = []
    for k in buckets:
        rs = bucket_oos[k]
        dsr = deflated_sharpe(rs, n_trials=n_trials, sr_var_across_trials=sr_var)
        folds_pos = sum(bucket_fold_pos[k])
        rows.append({
            "bucket": dict(zip(FACTOR_KEYS, k)),
            "n": len(rs),
            "net_mean": mean(rs),
            "ci95_lo": _ci95_lo(rs),
            "sharpe": _sharpe(rs),
            "dsr": dsr,
            "folds_pos": f"{folds_pos}/{len(bucket_fold_pos[k])}",
        })
    rows.sort(key=lambda r: r["dsr"], reverse=True)

    print("\n=== Top factor buckets (OOS, net of cost) ===")
    print(f"{'bucket':50s} {'n':>5s} {'mean':>9s} {'CIlo':>9s} {'SR':>6s} {'DSR':>6s} folds")
    for r in rows[:15]:
        bk = ",".join(f"{k}={v}" for k, v in r["bucket"].items())
        print(f"{bk:50s} {r['n']:5d} {r['net_mean']:+.5f} {r['ci95_lo']:+.5f} "
              f"{r['sharpe']:+.3f} {r['dsr']:.3f} {r['folds_pos']}")

    # ---- family-level gates (PBO / SPA) ----
    matrix = [bucket_oos[k] for k in buckets]
    pbo = cscv_pbo(matrix, n_blocks=10) if len(matrix) >= 2 else {"pbo": None}
    spa = spa_pvalue(matrix, n_boot=2000) if len(matrix) >= 2 else {"p_consistent": None}

    # ---- event-window correlation sanity check (small N, not a deploy path) ----
    print("\n=== Event-window forward-return correlation (sanity, small N) ===")
    for ev in ("nfp", "fomc", "cpi"):
        sub = feat[feat[ev] == 1]
        nsub = feat[feat[ev] == 0]
        if len(sub) < 5:
            print(f"  {ev}: n={len(sub)} (too few)")
            continue
        m_e = sub["fwd_ret"].mean(); m_n = nsub["fwd_ret"].mean()
        print(f"  {ev}: n={len(sub):4d} mean_fwd={m_e:+.5f}  vs non-event n={len(nsub)} mean={m_n:+.5f}  diff={m_e-m_n:+.5f}")

    # ---- event-trade walk-forward backtest (honest OOS, not just IS correlation) ----
    # For each event type, learn the trade direction on TRAIN (sign of conditional
    # mean forward return), apply it OOS on TEST, hold 4h, net of cost. Tiny N
    # per fold -> DSR will be weak; this is the honest conversion of the IS
    # correlation into an OOS verdict.
    print("\n=== Event-trade walk-forward backtest (OOS, net of cost) ===")
    event_results = []
    for ev in ("nfp", "fomc", "cpi"):
        oos_rs: list[float] = []
        fold_pos = []
        for tr_s, tr_e, te_s, te_e in folds:
            train = feat[(feat.index >= tr_s) & (feat.index < tr_e)]
            test = feat[(feat.index >= te_s) & (feat.index < te_e)]
            tr_ev = train[train[ev] == 1]
            if len(tr_ev) < 3:
                continue
            direction = 1 if tr_ev["fwd_ret"].mean() > 0 else -1
            te_ev = test[test[ev] == 1]
            net = (direction * te_ev["fwd_ret"] - args.cost_rt).tolist()
            oos_rs.extend(net)
            fold_pos.append(mean(net) > 0 if net else False)
        if len(oos_rs) < 5:
            print(f"  {ev}: OOS n={len(oos_rs)} (too few to test)")
            event_results.append({"event": ev, "n": len(oos_rs), "verdict": "too_few"})
            continue
        dsr = deflated_sharpe(oos_rs, n_trials=3, sr_var_across_trials=0.0)
        ci = _ci95_lo(oos_rs)
        fp = sum(fold_pos)
        print(f"  {ev}: OOS n={len(oos_rs):3d} mean={mean(oos_rs):+.5f} CIlo={ci:+.5f} "
              f"DSR={dsr:.3f} folds_pos={fp}/{len(fold_pos)}")
        event_results.append({
            "event": ev, "n": len(oos_rs), "oos_mean": mean(oos_rs),
            "ci95_lo": ci, "dsr": dsr, "folds_pos": f"{fp}/{len(fold_pos)}",
        })

    # ---- verdict ----
    winners = [r for r in rows if r["dsr"] >= 0.95 and r["ci95_lo"] > 0 and "+" in r["folds_pos"].split("/")[0] and int(r["folds_pos"].split("/")[0]) >= math.ceil(args.folds / 2)]
    verdict = {
        "target": args.target,
        "window": [str(feat.index.min()), str(feat.index.max())],
        "h1_rows": len(feat),
        "buckets_scanned": len(all_buckets),
        "buckets_traded_oos": len(buckets),
        "n_trials_for_deflation": n_trials,
        "cost_rt": args.cost_rt,
        "hold_hours": HOLD_HOURS,
        "family_pbo": pbo.get("pbo"),
        "family_spa_p": spa.get("p_consistent"),
        "winners": winners,
        "event_trade_backtest": event_results,
        "top_buckets": rows[:10],
        "method_caveats": [
            "close-to-close fixed 4h hold = OPTIMISTIC bound (no intrabar SL/TP)",
            "event windows use a constructed calendar (approximate, not a feed)",
            "cross-asset factors are venue-internal (oil/btc 1h return), no external macro",
            "factor buckets are joint-tuple scans; n_trials deflates for the scan size",
        ],
    }
    args.out.write_text(json.dumps(verdict, indent=2, default=str), encoding="utf-8")
    print(f"\nFamily PBO={pbo.get('pbo')}  SPA p={spa.get('p_consistent')}")
    print(f"ORGANIC WINNERS (DSR>=0.95, CI95 lo>0, majority folds): {len(winners)}")
    if winners:
        for w in winners:
            print(f"  WINNER: {w['bucket']} n={w['n']} mean={w['net_mean']:+.5f} DSR={w['dsr']:.3f}")
    else:
        print("  (none) -- no macro/environmental factor bucket has a positive OOS edge")
        print("          that survives the multiple-testing correction net of retail cost.")
    print(f"Report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())