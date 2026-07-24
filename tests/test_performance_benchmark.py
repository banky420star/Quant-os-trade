"""Tests for multi-symbol benchmark runner using real replay outputs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(r"C:\Users\ADMINI~1\AppData\Local\Temp\3\grok-goal-5d7c28d4f228\implementer")
sys.path.insert(0, str(ROOT))

from core.account_mode import performance_gates_active, runtime_mode_summary
from core.performance_benchmark import (
    BENCHMARK_EXECUTION_KEYS,
    apply_benchmark_config,
    benchmark_config_diff,
    format_benchmark_report,
    resolve_benchmark_replay_params,
    run_multi_symbol_benchmark,
    sync_performance_gates,
)
from core.performance_projection import benchmark_meets_target

VERDICT_REQUIRED_KEYS = frozenset({
    "refuted",
    "findings",
    "evidence",
    "confidence",
    "blocking",
    "details_md",
})
from core.replay_engine import run_portfolio_replay
from core.utils import load_config


def test_demo_mode_skips_performance_gates(growth_profile):
    cfg = load_config()
    assert cfg["mt5"]["account_mode"] == "demo"
    assert performance_gates_active(cfg) is False
    assert cfg["execution"]["starting_cash"] == 100
    assert cfg["practice"]["growth"]["enabled"] is True
    assert cfg["signals"]["default_risk_percent"] == 2.5
    assert cfg["session_scoring"]["enabled"] is False
    assert cfg["quant"]["strategy_ranking_enabled"] is False
    mode = runtime_mode_summary(cfg)
    assert mode["label"] == "growth"
    assert mode["growth_plan_active"] is True
    assert mode["performance_plan_active"] is False


def test_real_mode_syncs_performance_gates():
    import copy

    base = load_config()
    real_cfg = copy.deepcopy(base)
    real_cfg["mt5"]["account_mode"] = "real"
    # Base config ships apply_when: never (2026-07-21, so the $50k-plan gates
    # don't override the micro-live tuning); force it on to test the sync path.
    real_cfg["performance"]["apply_when"] = "real"
    cfg = sync_performance_gates(real_cfg)
    assert performance_gates_active(cfg) is True
    assert cfg["trading"]["aggressive_mode"] is False
    assert cfg["signals"]["default_risk_percent"] == cfg["performance"]["default_risk_percent"]
    assert cfg["risk"]["max_symbol_exposure_usd"] == cfg["performance"]["max_symbol_exposure_usd"]
    assert cfg["risk"]["max_total_exposure_usd"] == cfg["performance"]["max_total_exposure_usd"]
    assert cfg["execution"]["starting_cash"] == cfg["performance"]["projection_capital_usd"]
    assert cfg["intelligence"]["regime_veto_enabled"] is True


def test_benchmark_config_diff_is_execution_only():
    base = load_config()
    bench = apply_benchmark_config(base)
    diff = benchmark_config_diff(base, bench)
    assert set(diff) == {"execution"}
    assert set(diff["execution"]) <= BENCHMARK_EXECUTION_KEYS
    assert bench["execution"]["mode"] == "paper"
    assert bench["execution"]["starting_cash"] == base["performance"]["projection_capital_usd"]
    assert bench["trading"] == base["trading"]
    assert bench["signals"] == base["signals"]
    assert bench["risk"] == base["risk"]


def test_portfolio_replay_shared_equity():
    cfg = apply_benchmark_config(load_config())
    cfg["performance"] = {**cfg.get("performance", {}), "replay_max_bars": 120, "replay_step": 20}
    out = run_portfolio_replay(
        cfg,
        ["XAUUSDm", "USOILm"],
        max_bars=120,
        step=20,
    )
    assert out["bars_replayed"] > 0
    assert len(out["symbol_results"]) == 2
    assert all(r["bars_replayed"] == out["bars_replayed"] for r in out["symbol_results"])
    # True accounting identity of the PaperBroker: final_equity = cash +
    # unrealized_open. Open positions may be UNDERWATER at replay end, so
    # unrealized_open can be negative -- the prior `final >= starting +
    # pnl_total - 0.01` assertion falsely assumed unrealized >= 0 and failed
    # whenever a position was open against the entry. Check the exact identity
    # instead (and that cash reconciles with starting + realized, within the
    # sub-cent-per-trade rounding aggregation of pnl_total).
    assert abs(out["final_equity"] - (out["cash"] + out["unrealized_open"])) <= 0.01
    assert abs(out["cash"] - (out["starting_cash"] + out["pnl_total"])) <= 0.01 * max(1, out.get("trades_closed", 0))


def test_run_multi_symbol_benchmark_real_replay():
    base = load_config()
    base["performance"] = {
        **base.get("performance", {}),
        "replay_max_bars": 120,
        "replay_step": 20,
    }
    result = run_multi_symbol_benchmark(
        base,
        symbols=["XAUUSDm"],
        max_bars=120,
        step=20,
    )
    assert result["capital_base_usd"] == load_config()["performance"]["projection_capital_usd"]
    assert result["target_monthly_pnl_usd"] == 50_000
    assert len(result["symbol_results"]) == 1
    assert result["projection"]["combined_bars_replayed"] == result["portfolio"]["bars_replayed"]
    assert result["projection"]["portfolio_mode"] == "shared_equity"
    report = format_benchmark_report(result)
    assert "capital_base_usd" in report
    assert "projected_monthly_pnl" in report


def test_resolve_benchmark_replay_params_full_history(growth_profile):
    """On-disk parquets span well beyond the short-window era (~71 bars)."""
    cfg = load_config()
    resolved = resolve_benchmark_replay_params(cfg)
    assert resolved["estimated_bars_replayed"] > 200
    assert resolved["estimated_replay_window_days"] >= 7.0


def test_benchmark_eligibility_rejects_short_window():
    projection = {
        "replay_window_days": 2.5,
        "projected_monthly_pnl": 80_000.0,
    }
    elig = benchmark_meets_target(projection, trades_closed=10)
    assert elig["pnl_ok"] is True
    assert elig["coverage_ok"] is False
    assert elig["meets_target"] is False


def test_cli_benchmark_subprocess(tmp_path, growth_profile):
    """Shipped entrypoint: scripts/run_performance_benchmark.py."""
    import os

    out_path = tmp_path / "benchmark_out.json"
    env = {**os.environ, "MT5_QUANT_PROFILE": "growth"}
    # Short window keeps CI fast; full-history depth is asserted in
    # test_resolve_benchmark_replay_params_full_history.
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/run_performance_benchmark.py",
            "--max-bars",
            "120",
            "--step",
            "20",
            "--output",
            str(out_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "cli_benchmark_subprocess.log").write_text(
        proc.stdout + proc.stderr + f"\nexit_code={proc.returncode}",
        encoding="utf-8",
    )
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    cfg = load_config()
    capital = float(cfg["performance"]["projection_capital_usd"])
    assert cfg.get("active_profile") == "growth"
    assert payload["capital_base_usd"] == capital
    # BTCUSDm D1 is not served on every Exness server (e.g. MT5Trial9 has no
    # BTC parquet), so the benchmark CLI correctly drops it and reports only
    # the symbols it actually has data for. Assert against the symbols present
    # in this environment rather than a hard-coded 3, so the test passes on
    # servers without BTC while still failing if the core XAU+USOIL pair is
    # missing (the real regression we care about).
    tested = set(payload["symbols_tested"])
    assert {"XAUUSDm", "USOILm"}.issubset(tested), (
        f"core pair missing from symbols_tested: {sorted(tested)}"
    )
    assert {r["symbol"] for r in payload["symbol_results"]} == tested
    assert "eligibility" in payload
    assert "coverage_ok" in payload
    assert "pnl_ok" in payload
    assert float(payload["projection"]["replay_window_days"]) > 0
    assert int(payload["projection"].get("combined_bars_replayed", 0)) > 0
    assert "meets_target" in payload
    assert "projected_monthly_pnl" in payload


def test_run_verification_subprocess():
    """Shipped verification script writes verdict + summary to SCRATCH."""
    proc = subprocess.run(
        [sys.executable, "scripts/run_verification.py", "--scratch", str(SCRATCH)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    (SCRATCH / "run_verification_subprocess.log").write_text(
        proc.stdout + proc.stderr + f"\nexit_code={proc.returncode}",
        encoding="utf-8",
    )
    verdict = json.loads((SCRATCH / "verdict.json").read_text(encoding="utf-8"))
    goal_verdict = json.loads((SCRATCH / "goal-verdict.json").read_text(encoding="utf-8"))
    summary = json.loads((SCRATCH / "verification_summary.json").read_text(encoding="utf-8"))
    assert set(verdict) == VERDICT_REQUIRED_KEYS
    assert verdict == goal_verdict
    assert verdict["blocking"] == "none"
    assert isinstance(verdict["details_md"], str) and len(verdict["details_md"]) > 50
    assert isinstance(verdict["refuted"], bool)
    assert isinstance(summary["all_gating_passed"], bool)
    assert (SCRATCH / "performance_benchmark.log").exists()
    assert (SCRATCH / "pytest_results.log").exists()
    assert (SCRATCH / "start_launch.log").exists()
    step2 = next(s for s in summary["verification_plan"] if s.get("step") == 2)
    assert "coverage_ok" in step2
    assert "eligibility" in step2