"""Validate bot team coordination. Exit 0 when all gates pass."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.coordination import check_coordination  # noqa: E402
from core.utils import write_json_state  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="MT5 Quant OS coordination check")
    parser.add_argument("--json", action="store_true", help="Print full JSON report")
    parser.add_argument("--max-age", type=float, default=180.0, help="Max state file age (seconds)")
    parser.add_argument("--write-state", action="store_true", help="Persist report to state/coordination.json")
    args = parser.parse_args()

    report = check_coordination(max_state_age_seconds=args.max_age)
    if args.write_state:
        write_json_state("coordination.json", report)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        status = "COORDINATED" if report["coordinated"] else "NOT COORDINATED"
        print(f"\n  Team status: {status}")
        print(f"  Checks: {len(report['checks'])} | errors: {report['error_count']}")
        print("  ─────────────────────────────────────────")
        for c in report["checks"]:
            mark = "✓" if c["ok"] else "✗"
            print(f"  {mark} {c['name']}: {c['detail']}")
        print()

    return 0 if report["coordinated"] else 1


if __name__ == "__main__":
    raise SystemExit(main())