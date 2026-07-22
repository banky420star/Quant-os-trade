"""One-off feasibility check: does Exness serve >=3y of XAUUSDm D1 bars?

Read-only history download (no trading). Determines whether the D1 trend-variant
redirect (see VERDICT.md §7 + D1 research plan) is unblocked on data, since a
DSR-eligible D1 verdict needs ~3-5y of D1 history (~40-80 trades at Donchian-20
frequency). Prints: bar count, first/last D1 bar, year span, ATR(D1,14) median.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_collector import DataCollector
from core.history_manager import HistoryManager, HISTORY_DIR
from core.mt5_connection_manager import MT5ConnectionManager
from core.symbol_manager import SymbolManager
from core.utils import ensure_dirs, load_config, read_json_state, setup_logger


def main() -> int:
    ensure_dirs()
    config = load_config()
    logger = setup_logger("check_d1", "check_d1.log")
    broker = read_json_state("broker_symbols.json", default={})
    symbol_map = broker.get("resolved", {})
    target = "XAUUSDm"

    conn = MT5ConnectionManager(config, logger)
    try:
        if not conn.connect():
            print("MT5_CONNECT_FAILED: cannot reach terminal (D1 depth check blocked on connection)")
            return 2
        if not symbol_map:
            sm = SymbolManager(config, logger)
            symbol_map = sm.discover()["resolved"]
        sm = SymbolManager(config, logger)
        sm.set_resolved(symbol_map)
        collector = DataCollector(config, conn, sm, logger)
        history = HistoryManager(config, collector, logger)
        print(f"Downloading D1 for {target} ...")
        history.download_full(symbol_map, "D1")
        pq = history.parquet_path(target, "D1")
        if not pq.exists():
            print(f"NO_PARQUET: {pq} not written")
            return 3
        import pandas as pd
        df = pd.read_parquet(pq)
        if len(df) == 0:
            print("EMPTY: 0 D1 bars")
            return 4
        t = pd.to_datetime(df["time"])
        span_years = (t.iloc[-1] - t.iloc[0]).total_seconds() / (365.25 * 86400)
        # ATR(14) on D1 for cost_r estimate
        h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
        pc = c.shift(1)
        tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        atr14 = tr.ewm(alpha=1 / 14, adjust=False).mean().median()
        print(f"D1_BARS={len(df)}")
        print(f"FIRST={t.iloc[0]}  LAST={t.iloc[-1]}")
        print(f"SPAN_YEARS={span_years:.2f}")
        print(f"ATR14_MEDIAN={atr14:.4f}  1.5xATR_stop={1.5*atr14:.4f}")
        spot = float(c.median())
        cost_price = 0.0030 * spot
        cost_r = cost_price / (1.5 * atr14) if atr14 > 0 else float("nan")
        print(f"SPOT_MEDIAN={spot:.2f}  cost_price@30bps={cost_price:.4f}  cost_r@30bps={cost_r:.4f}R")
        if span_years >= 3.0:
            print("VERDICT: D1 depth >= 3y -> D1 redirect UNBLOCKED on data")
        else:
            print(f"VERDICT: D1 depth {span_years:.2f}y < 3y -> D1 redirect BLOCKED on data (need >=3y)")
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