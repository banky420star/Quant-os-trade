"""Long-running equity observe loop for goal babysitting.

Writes one JSON line per cycle to SCRATCH observe_log and prints equity.
Does not fabricate equity. Stops early if equity >= 500000.

Usage:
  python scripts/babysit_observe_loop.py --cycles 40 --interval 45
  python scripts/babysit_observe_loop.py --out-dir path --cycles 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SCRATCH = Path(
    r"C:\Users\ADMINI~1\AppData\Local\Temp\grok-goal-51c106e6bd52\implementer\observe_log"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=40)
    ap.add_argument("--interval", type=float, default=45.0)
    ap.add_argument("--out-dir", type=str, default=str(DEFAULT_SCRATCH))
    ap.add_argument("--profile", default="100")
    args = ap.parse_args()
    if args.profile:
        os.environ["MT5_QUANT_PROFILE"] = str(args.profile)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    series = out_dir / "equity_series.jsonl"
    latest = out_dir / "latest.json"

    from core.utils import read_json_state, utc_now_iso
    from loops import babysit_loop

    for i in range(1, args.cycles + 1):
        try:
            st = babysit_loop.run()
        except Exception as exc:  # noqa: BLE001
            st = {"error": str(exc), "actions": []}

        acc = read_json_state("account.json", default={}) or {}
        try:
            eq = float(acc.get("equity") or 0)
        except (TypeError, ValueError):
            eq = 0.0
        line = {
            "cycle": i,
            "ts": utc_now_iso(),
            "equity": eq,
            "balance": acc.get("balance"),
            "ge500k": eq >= 500_000,
            "actions": st.get("actions"),
            "expectancy_r": st.get("expectancy_r"),
            "payoff_ratio": st.get("payoff_ratio"),
            "supervisor_ok": st.get("supervisor_ok"),
            "error": st.get("error"),
        }
        print(json.dumps(line), flush=True)
        with series.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        latest.write_text(json.dumps(line, indent=2), encoding="utf-8")

        if eq >= 500_000:
            print(json.dumps({"done": True, "equity": eq, "message": "equity >= 500000"}), flush=True)
            return 0
        if i < args.cycles:
            time.sleep(max(5.0, float(args.interval)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
