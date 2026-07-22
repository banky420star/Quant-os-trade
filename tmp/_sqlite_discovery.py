"""SQLite discovery probe — finds trade-like tables in state/quant_os.db.

Used to surface the historic closed-trade archive that scripts/build_trade_log.py
does NOT currently consult (it only reads paper_trades.json + paper_orders.json
+ MT5 deal history). The dashboard's "0 trades" complaint stems from build_log
returning 0 records when those three sources are empty in a brand-new session,
even though state/quant_os.db holds hundreds of historic closes.

This script is read-only, prints a summary, and optionally writes a small
discovery report to state/sqlite_discovery.json so downstream code can use it.

Usage:
    python tools/sqlite_discovery.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# Run from inside mt5_quant_agent/, so use CWD-relative path to state/.
PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", ".")).resolve()
CWD = Path(os.getcwd()).resolve()
# The script was written as tmp/_sqlite_discovery.py and we expect CWD to be
# the project we care about. Pick whichever has state/quant_os.db.
for candidate in (CWD, CWD.parent / CWD.name if CWD.name else CWD, Path(".").resolve(), Path("../mt5_quant_agent").resolve()):
    if (candidate / "state" / "quant_os.db").exists():
        ROOT = candidate
        break
else:
    # Fallback: assume script is sibling of state/ dir in some worktree.
    ROOT = CWD
DB = ROOT / "state" / "quant_os.db"
STATE = ROOT / "state"


def main() -> int:
    if not DB.exists():
        print(f"DB MISSING at {DB}")
        return 1
    print(f"DB size: {os.path.getsize(DB):,} bytes")

    conn = sqlite3.connect(str(DB), timeout=5)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in cur.fetchall()]
        print(f"TABLES ({len(tables)}):")
        for t in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM [{t}]")
                n = cur.fetchone()[0]
            except Exception as e:  # noqa: BLE001
                n = f"ERR({e})"
            print(f"  {t:<40} rows={n}")

        # Find trade-like tables (substring match on common trade columns).
        trade_candidates = []
        for t in tables:
            try:
                cur.execute(f"PRAGMA table_info([{t}])")
                cols = [r[1] for r in cur.fetchall()]
            except Exception:
                continue
            colset = " ".join(cols).lower()
            if any(k in colset for k in ("pnl", "result", "r_multiple", "exit_reason", "close_time", "magic", "trade_id", "symbol", "ticket")):
                trade_candidates.append({"table": t, "cols": cols, "row_count": 0})
                try:
                    cur.execute(f"SELECT COUNT(*) FROM [{t}]")
                    trade_candidates[-1]["row_count"] = cur.fetchone()[0]
                except Exception:
                    pass
        print()
        print(f"TRADE-LIKE TABLES ({len(trade_candidates)}):")
        for c in trade_candidates:
            print(f"  {c['table']} rows={c['row_count']} cols={len(c['cols'])}")

        # Sample one row from each candidate so we know the actual shape.
        print()
        print("SAMPLES (first row of each trade-like table):")
        for c in trade_candidates[:8]:
            try:
                cur.execute(f"SELECT * FROM [{c['table']}] LIMIT 1")
                row = cur.fetchone()
                cols = [d[0] for d in cur.description]
                sample = {col: (str(val) if val is not None else None) for col, val in zip(cols, row)}
                # Truncate long string fields.
                sample = {k: (v[:60] + "..." if isinstance(v, str) and len(v) > 60 else v) for k, v in sample.items()}
                print(f"  {c['table']}:")
                for k, v in list(sample.items())[:25]:
                    print(f"      {k:<30} = {v}")
            except Exception as e:  # noqa: BLE001
                print(f"  {c['table']}: SAMPLE_ERR {e}")

        # Persist discovery report.
        report = {
            "db_path": str(DB),
            "db_size": os.path.getsize(DB),
            "tables": [{"table": t, "rows": 0} for t in tables],
            "trade_like": trade_candidates,
            "probed_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        }
        for entry in report["tables"]:
            try:
                cur.execute(f"SELECT COUNT(*) FROM [{entry['table']}]")
                entry["rows"] = cur.fetchone()[0]
            except Exception:
                entry["rows"] = -1
        out = STATE / "sqlite_discovery.json"
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print()
        print(f"WROTE state/sqlite_discovery.json ({len(trade_candidates)} trade candidates)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
