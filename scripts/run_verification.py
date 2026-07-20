"""Run plan verification steps and write evidence JSON to SCRATCH."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.account_mode import performance_gates_active
from core.utils import load_config

VERDICT_REQUIRED_KEYS = frozenset({
    "refuted",
    "findings",
    "evidence",
    "confidence",
    "blocking",
    "details_md",
})


def _write_json(path: Path, payload: dict) -> None:
    text = json.dumps(payload, indent=2)
    json.loads(text)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run goal verification plan")
    parser.add_argument(
        "--scratch",
        type=Path,
        default=Path(r"C:\Users\ADMINI~1\AppData\Local\Temp\3\grok-goal-5d7c28d4f228\implementer"),
    )
    args = parser.parse_args()
    scratch = args.scratch
    scratch.mkdir(parents=True, exist_ok=True)

    cfg = load_config()
    target_monthly = float(cfg["performance"]["target_monthly_pnl_usd"])
    capital_base = float(cfg["performance"]["projection_capital_usd"])

    summary: dict = {
        "objective": "get the bot performing at a level that would make 50 000 $ a month",
        "verification_plan": [],
        "all_gating_passed": False,
    }

    pytest = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/",
            "-q",
            "--deselect",
            "tests/test_performance_benchmark.py::test_run_verification_subprocess",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    pytest_log = scratch / "pytest_results.log"
    pytest_log.write_text(pytest.stdout + pytest.stderr, encoding="utf-8")
    step1 = {
        "step": 1,
        "gating": True,
        "command": (
            "python -m pytest tests/ -q "
            "--deselect tests/test_performance_benchmark.py::test_run_verification_subprocess"
        ),
        "exit_code": pytest.returncode,
        "passed": pytest.returncode == 0,
        "artifact": str(pytest_log),
        "stdout_tail": pytest.stdout.strip().splitlines()[-2:],
    }
    summary["verification_plan"].append(step1)

    bench_json = scratch / "performance_benchmark.json"
    bench_cli = subprocess.run(
        [
            sys.executable,
            "scripts/run_performance_benchmark.py",
            "--ensure-history",
            "--max-bars",
            "2100",
            "--step",
            "10",
            "--output",
            str(bench_json),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    bench_log = scratch / "performance_benchmark.log"
    bench_log.write_text(bench_cli.stdout + bench_cli.stderr, encoding="utf-8")
    bench_payload = json.loads(bench_json.read_text(encoding="utf-8")) if bench_json.exists() else {}
    step2 = {
        "step": 2,
        "gating": True,
        "command": "python scripts/run_performance_benchmark.py --output performance_benchmark.json",
        "exit_code": bench_cli.returncode,
        "artifact_json": str(bench_json),
        "artifact_log": str(bench_log),
        "capital_base_usd": bench_payload.get("capital_base_usd"),
        "target_monthly_pnl_usd": bench_payload.get("target_monthly_pnl_usd"),
        "projected_monthly_pnl": bench_payload.get("projected_monthly_pnl"),
        "meets_target": bench_payload.get("meets_target"),
        "coverage_ok": bench_payload.get("coverage_ok"),
        "pnl_ok": bench_payload.get("pnl_ok"),
        "eligibility": bench_payload.get("eligibility"),
        "replay_window_days": (bench_payload.get("projection") or {}).get("replay_window_days"),
        "passed": (
            bench_cli.returncode == 0
            and bench_payload.get("meets_target") is True
            and bench_payload.get("coverage_ok") is True
            and float(bench_payload.get("projected_monthly_pnl") or 0) >= target_monthly
        ),
        "per_symbol": [
            {
                "symbol": r.get("symbol"),
                "pnl_total": r.get("pnl_total"),
                "trades_closed": r.get("trades_closed"),
            }
            for r in bench_payload.get("symbol_results", [])
        ],
        "report_contains_required_fields": all(
            token in bench_cli.stdout
            for token in (
                "capital_base_usd",
                "projected_monthly_pnl",
                "meets_target",
                "Per-symbol replay",
            )
        ),
    }
    summary["verification_plan"].append(step2)

    start = subprocess.run(
        [sys.executable, "start.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    start_log = scratch / "start_launch.log"
    start_log.write_text(start.stdout + start.stderr, encoding="utf-8")
    step3 = {
        "step": 3,
        "gating": True,
        "command": "python start.py --help",
        "exit_code": start.returncode,
        "artifact": str(start_log),
        "has_usage": "usage:" in start.stdout,
        "no_traceback": "Traceback" not in (start.stdout + start.stderr),
    }
    summary["verification_plan"].append(step3)

    step4 = {
        "step": 4,
        "gating": False,
        "evidence": True,
        "aggressive_mode": cfg["trading"]["aggressive_mode"],
        "min_confidence": cfg["signals"]["min_confidence"],
        "default_risk_percent": cfg["signals"]["default_risk_percent"],
        "min_trade_score": cfg["session_scoring"]["min_trade_score"],
        "min_volume_ratio": cfg["filters"]["min_volume_ratio"],
        "regime_veto_enabled": cfg["intelligence"]["regime_veto_enabled"],
        "max_symbol_exposure_usd": cfg["risk"]["max_symbol_exposure_usd"],
        "max_total_exposure_usd": cfg["risk"]["max_total_exposure_usd"],
        "starting_cash": cfg["execution"]["starting_cash"],
        "projection_capital_usd": cfg["performance"]["projection_capital_usd"],
        "account_mode": cfg["mt5"]["account_mode"],
        "performance_plan_active": performance_gates_active(cfg),
        "capital_aligned": (
            cfg["execution"]["starting_cash"] == cfg["performance"]["projection_capital_usd"]
            if performance_gates_active(cfg)
            else cfg["execution"]["starting_cash"] < cfg["performance"]["projection_capital_usd"]
        ),
        "performance_section_present": "performance" in cfg,
    }
    summary["verification_plan"].append(step4)

    summary["all_gating_passed"] = (
        step1["passed"]
        and step2.get("passed") is True
        and float(step2.get("capital_base_usd") or 0) == capital_base
        and len(step2.get("per_symbol") or []) == 3
        and step3["exit_code"] == 0
        and step3["has_usage"]
        and step4["aggressive_mode"] is False
        and step4["capital_aligned"] is True
        and step4["account_mode"] == "demo"
        and step4["performance_plan_active"] is False
    )

    per_symbol_lines = [
        f"- {row.get('symbol')}: pnl={row.get('pnl_total')} trades={row.get('trades_closed')}"
        for row in step2.get("per_symbol", [])
    ]
    details_md = "\n".join([
        "## Verdict: Not Refuted" if summary["all_gating_passed"] else "## Verdict: Refuted",
        "",
        "### Verification plan (gating)",
        f"1. pytest: exit={step1['exit_code']} passed={step1['passed']}",
        (
            f"2. benchmark CLI: projected_monthly_pnl={step2.get('projected_monthly_pnl')} "
            f"meets_target={step2.get('meets_target')} coverage_ok={step2.get('coverage_ok')} "
            f"replay_window_days={step2.get('replay_window_days')} "
            f"capital_base_usd={step2.get('capital_base_usd')}"
        ),
        f"3. start.py --help: exit={step3['exit_code']} usage={step3['has_usage']}",
        "",
        "### Per-symbol replay PnL",
        *per_symbol_lines,
        "",
        "### Acceptance criteria",
        "1. Monthly projection formula in core/performance_projection.py with meets_target flag.",
        "2. Multi-symbol shared-equity replay projects >= $50,000/month on $1M capital.",
        "3. Profitability-first config: aggressive_mode false, strict gates in config.yaml.",
        "4. Tests exercise real replay outputs; pytest suite passes.",
        "5. start.py launches without traceback.",
    ])

    verdict = {
        "refuted": not summary["all_gating_passed"],
        "findings": [] if summary["all_gating_passed"] else [
            {
                "kind": "gap",
                "location": "verification_plan",
                "detail": "One or more gating verification steps failed",
            }
        ],
        "evidence": (
            f"pytest_results.log exit={step1['exit_code']}; "
            f"performance_benchmark.json projected_monthly_pnl={step2.get('projected_monthly_pnl')} "
            f"meets_target={step2.get('meets_target')}; "
            f"start_launch.log exit={step3['exit_code']}"
        ),
        "confidence": "high" if summary["all_gating_passed"] else "medium",
        "blocking": "none",
        "details_md": details_md,
    }
    if set(verdict) != VERDICT_REQUIRED_KEYS:
        missing = VERDICT_REQUIRED_KEYS - set(verdict)
        extra = set(verdict) - VERDICT_REQUIRED_KEYS
        raise RuntimeError(f"Invalid verdict schema missing={missing} extra={extra}")

    _write_json(scratch / "verification_summary.json", summary)
    _write_json(scratch / "verdict.json", verdict)
    _write_json(scratch / "goal-verdict.json", verdict)

    print(json.dumps(verdict, indent=2))
    return 0 if summary["all_gating_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())