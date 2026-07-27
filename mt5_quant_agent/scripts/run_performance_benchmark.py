"""Run multi-symbol replay benchmark and print monthly PnL projection."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.performance_benchmark import format_benchmark_report, run_multi_symbol_benchmark, write_benchmark_log
from core.utils import load_config, setup_logger

SCRATCH = Path(r"C:\Users\ADMINI~1\AppData\Local\Temp\3\grok-goal-5d7c28d4f228\implementer")


def main() -> int:
    parser = argparse.ArgumentParser(description="MT5 Quant performance benchmark")
    parser.add_argument("--output", type=Path, default=None, help="JSON log path")
    parser.add_argument("--max-bars", type=int, default=None)
    parser.add_argument("--step", type=int, default=None)
    parser.add_argument(
        "--ensure-history",
        action="store_true",
        help="Backfill parquet history before replay (logs to SCRATCH/benchmark_history.json)",
    )
    args = parser.parse_args()

    logger = setup_logger("performance_benchmark", "performance_benchmark.log")
    if args.ensure_history:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ensure_benchmark_history",
            ROOT / "scripts" / "ensure_benchmark_history.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.ensure_history(load_config(), scratch=SCRATCH, logger=logger)

    result = run_multi_symbol_benchmark(max_bars=args.max_bars, step=args.step, logger=logger)
    report = format_benchmark_report(result)
    print(report)

    if args.output:
        write_benchmark_log(result, args.output)
    else:
        print(json.dumps({"meets_target": result["meets_target"], "projected_monthly_pnl": result["projected_monthly_pnl"]}))

    return 0 if result.get("meets_target") else 1


if __name__ == "__main__":
    raise SystemExit(main())