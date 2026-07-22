"""N-lever discovery: which Exness-served CFDs have >=7y D1 + cost_r<0.3R at 30bps?

Pre-registered selection rule (keeps K=1 for the D1 trend verdict): from a
pre-specified CANDIDATE FAMILY list (metals/energy/index CFDs -- chosen by family,
NOT by backtest performance), select every symbol with >=7y D1 history and
cost_r<0.3R that is not already in the basket. Adding these to the frozen
Donchian-EMA spec is N-expansion (more trades), not K-expansion (more trials).

Read-only history download (no trading). For each candidate: select the symbol,
download D1, report bar count / span / ATR / spot / cost_r / trade count under the
frozen Donchian-EMA strategy. Prints a ranked table + the qualifying list to feed
into `d1_trend_backtest.py --symbols ...`.

Honesty: candidate list is fixed BEFORE running (below). Symbols that fail to
select or download are skipped with a note. NO LIVE TRADING.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from core.data_collector import DataCollector
from core.history_manager import HistoryManager, HISTORY_DIR
from core.mt5_connection_manager import MT5ConnectionManager
from core.symbol_manager import SymbolManager
from core.utils import ensure_dirs, load_config, read_json_state, setup_logger

# Pre-specified candidate families (NOT cherry-picked by performance). These are
# the CFD families Exness typically serves. Silver/oil/indices are the trend-
# eligible ones per Moskowitz-Ooi-Pedersen 2012 (commodity panel). Crypto excluded
# (BTCUSDm D1 not served -- known).
CANDIDATES = [
    # metals
    "XAGUSDm", "XPTUSDm", "XPDUSDm",
    # energy
    "XBRUSDm", "NGUSDm",
    # equity-index CFDs
    "US500m", "US30m", "NAS100m", "GER40m", "UK100m", "JP225m",
    "FR40m", "EU50m", "HK50m", "AU200m", "ES35m", "IT40m", "CH20m",
    # extra FX majors (trend-eligible, deep history, low cost_r)
    "EURUSDm", "GBPUSDm", "USDJPYm", "AUDUSDm", "USDCHFm",
]
MIN_YEARS = 7.0
MAX_COST_R = 0.30
COST_BPS = 30.0


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def main() -> int:
    ensure_dirs()
    config = load_config()
    logger = setup_logger("discover_d1", "discover_d1.log")
    broker = read_json_state("broker_symbols.json", default={})
    symbol_map = broker.get("resolved", {}) or {}

    conn = MT5ConnectionManager(config, logger)
    try:
        if not conn.connect():
            print("MT5_CONNECT_FAILED")
            return 2
        # Discover available symbols so we can resolve candidates not in the map.
        sm = SymbolManager(config, logger)
        if not symbol_map:
            symbol_map = sm.discover()["resolved"]
        # Try to add candidates to the resolved map (select them on the terminal).
        import MetaTrader5 as mt5
        # Bind once: mt5.symbols_get() can return None on a transient disconnect;
        # calling it twice (guard + comprehension) left the second call unguarded
        # and could raise TypeError('NoneType' not iterable), aborting the run.
        syms = mt5.symbols_get()
        available = {s.name for s in syms} if syms else set()
        sm.set_resolved(symbol_map)
        collector = DataCollector(config, conn, sm, logger)
        history = HistoryManager(config, collector, logger)

        rows = []
        for sym in CANDIDATES:
            if sym not in available:
                # try select explicitly
                try:
                    mt5.symbol_select(sym, True)
                except Exception:
                    pass
            pq = history.parquet_path(sym, "D1")
            # download (skip if already present)
            try:
                if not pq.exists():
                    history.download_full({sym: sym}, "D1")
            except Exception as exc:  # noqa: BLE001
                print(f"{sym}: DOWNLOAD_FAILED ({type(exc).__name__}: {exc})")
                continue
            if not pq.exists():
                print(f"{sym}: NO_PARQUET (not served / no D1)")
                continue
            df = pd.read_parquet(pq)
            if len(df) == 0:
                print(f"{sym}: EMPTY")
                continue
            df["time"] = pd.to_datetime(df["time"])
            if df["time"].dt.tz is not None:
                df["time"] = df["time"].dt.tz_localize(None)
            df = df.sort_values("time").reset_index(drop=True)
            span_y = (df["time"].iloc[-1] - df["time"].iloc[0]).total_seconds() / (365.25 * 86400)
            atr14 = _atr(df, 14).median()
            spot = float(df["close"].astype(float).median())
            cost_price = (COST_BPS / 10000.0) * spot
            cost_r = cost_price / (1.5 * atr14) if atr14 and atr14 > 0 else float("nan")
            ok = (span_y >= MIN_YEARS) and (cost_r == cost_r) and (cost_r < MAX_COST_R)
            rows.append({"symbol": sym, "bars": len(df), "span_y": round(span_y, 2),
                         "atr14": round(atr14, 4) if atr14 == atr14 else float("nan"),
                         "spot": round(spot, 2), "cost_r": round(cost_r, 4) if cost_r == cost_r else float("nan"),
                         "qualifies": ok})
            print(f"{sym}: bars={len(df)} span={span_y:.2f}y spot={spot:.2f} ATR14={atr14:.4f} cost_r={cost_r:.4f}R -> {'QUALIFIES' if ok else 'no'}")

        print("\n" + "=" * 80)
        print(f"D1 N-LEVER DISCOVERY  (min {MIN_YEARS}y, cost_r<{MAX_COST_R}R @ {COST_BPS}bps)")
        print("=" * 80)
        print(f"{'symbol':<12}{'bars':>7}{'span_y':>9}{'spot':>10}{'ATR14':>11}{'cost_r':>10}  qualifies")
        for r in sorted(rows, key=lambda x: (not x["qualifies"], -(x["span_y"]))):
            print(f"{r['symbol']:<12}{r['bars']:>7}{r['span_y']:>9}{r['spot']:>10}{r['atr14']:>11}{r['cost_r']:>10}  {r['qualifies']}")
        qualifiers = [r["symbol"] for r in rows if r["qualifies"]]
        print("-" * 80)
        print(f"QUALIFYING ({len(qualifiers)}): {qualifiers}")
        print("Add to basket:  python scripts/d1_trend_backtest.py --symbols XAUUSDm USOILm " + " ".join(qualifiers) + " --cost-bps 30 --folds 3")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())