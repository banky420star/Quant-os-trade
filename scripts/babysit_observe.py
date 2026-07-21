"""One-shot babysit observe cycle — write snapshot JSON to stdout path.

Usage:
  python scripts/babysit_observe.py --out path/to/snapshot.json
  python scripts/babysit_observe.py --out snapshot.json --profile 100
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.babysit_metrics import observe_snapshot
from core.micro_profile import sync_micro_profile
from core.utils import load_config, read_json_state, utc_now_iso


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="Output JSON path")
    ap.add_argument("--profile", default=None)
    args = ap.parse_args()
    if args.profile:
        os.environ["MT5_QUANT_PROFILE"] = str(args.profile)

    config = load_config()
    config = sync_micro_profile(config)
    account = read_json_state("account.json", default={}) or {}
    supervisor = read_json_state("supervisor.json", default={}) or {}
    trade_log = read_json_state("trade_log.json", default={}) or {}
    gates = read_json_state("adaptive_gates.json", default={}) or {}

    snap = observe_snapshot(
        account=account if isinstance(account, dict) else {},
        supervisor=supervisor if isinstance(supervisor, dict) else {},
        trade_log=trade_log if isinstance(trade_log, dict) else {},
        config=config,
        gates=gates if isinstance(gates, dict) else {},
    )
    snap["timestamp"] = utc_now_iso()
    snap["profile"] = args.profile or os.environ.get("MT5_QUANT_PROFILE") or "auto"

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "equity": snap.get("equity"),
        "equity_ge_500000": snap.get("equity_ge_500000"),
        "supervisor_ok": snap.get("supervisor_ok"),
        "regressions": len(snap.get("regressions") or []),
        "critical": len(snap.get("critical_regressions") or []),
        "book_n": (snap.get("book") or {}).get("n"),
        "expectancy_r": (snap.get("book") or {}).get("expectancy_r"),
        "total_pnl": (snap.get("book") or {}).get("total_pnl"),
        "out": str(out),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
