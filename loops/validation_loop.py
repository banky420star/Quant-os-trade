"""Offline validation loop for repeated safety and profitability checks.

This loop is deliberately read-only with respect to trading state. It never
imports MT5, starts the bot, places orders, changes config, or mutates paper/
MT5 ledgers. Each cycle runs a bounded test command, reviews existing trade
metrics, performs static software checks, and validates 10,000 deterministic
samples against the persisted order/pipeline evidence.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import ensure_dirs, load_config, read_json_state, setup_logger, utc_now_iso, write_json_state

DEFAULT_INSTANCE_COUNT = 10_000
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_TEST_TIMEOUT_SECONDS = 120
_HISTORY_LIMIT = 100

# The recurring worker runs only this isolated validation-loop test. The full
# suite and state-touching integration tests remain manual/CI jobs; recurring
# validation must not compete with the trading process or mutate live state.
_DEFAULT_TEST_TARGETS = ("tests/test_validation_loop.py",)
_SAFE_TEST_TARGETS = frozenset(_DEFAULT_TEST_TARGETS)
_SOFTWARE_FILES = (
    "core/paper_broker.py",
    "core/mt5_broker.py",
    "core/verifier.py",
    "loops/verifier_loop.py",
    "loops/execution_loop.py",
    "core/exposure.py",
    "core/position_sizing.py",
)


def _safe_load(filename: str, default: Any) -> Any:
    """Read a state file without allowing a corrupt file to abort validation."""
    return read_json_state(filename, default=default)


def _orders_for_review() -> tuple[str, list[dict[str, Any]]]:
    """Choose a ledger without treating historical MT5 rows as paper."""
    mt5 = _safe_load("mt5_orders.json", {})
    if isinstance(mt5, dict) and mt5.get("mode") == "mt5":
        rows = mt5.get("orders") or []
        return "mt5_orders.json", [row for row in rows if isinstance(row, dict)]
    paper = _safe_load("paper_orders.json", {})
    if isinstance(paper, dict) and paper.get("mode") not in {"mt5", "live"}:
        rows = paper.get("orders") or []
        return "paper_orders.json", [row for row in rows if isinstance(row, dict)]
    # Do not feed a legacy/live ledger into an offline paper validation run.
    return "no_compatible_ledger", []


def _trades_for_review() -> tuple[str, list[dict[str, Any]]]:
    mt5 = _safe_load("mt5_trades.json", {})
    if isinstance(mt5, dict) and mt5.get("mode") == "mt5":
        rows = mt5.get("trades") or []
        return "mt5_trades.json", [row for row in rows if isinstance(row, dict)]
    paper = _safe_load("paper_trades.json", {})
    if isinstance(paper, dict) and paper.get("mode") not in {"mt5", "live"}:
        rows = paper.get("trades") or []
        return "paper_trades.json", [row for row in rows if isinstance(row, dict)]
    return "no_compatible_ledger", []


def _order_error(order: dict[str, Any]) -> str:
    return str(
        order.get("error")
        or order.get("failure_reason")
        or order.get("reject_reason")
        or order.get("rejection_reason")
        or ""
    )


def _profit_review() -> dict[str, Any]:
    """Review realized performance only; never infer a live trade instruction."""
    source, trades = _trades_for_review()
    pnls: list[float] = []
    wins = losses = 0
    for trade in trades:
        try:
            pnl = float(trade.get("pnl", trade.get("profit", 0)) or 0)
        except (TypeError, ValueError):
            continue
        pnls.append(pnl)
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
    total = round(sum(pnls), 2)
    avg = round(total / len(pnls), 4) if pnls else 0.0
    gross_win = sum(x for x in pnls if x > 0)
    gross_loss = abs(sum(x for x in pnls if x < 0))
    recommendations: list[str] = []
    if not pnls:
        recommendations.append("collect more closed trades before changing strategy or risk settings")
    elif avg <= 0:
        recommendations.append("do not auto-increase risk; review losing setups and exit reasons manually")
    if gross_loss and gross_win / gross_loss < 1.0:
        recommendations.append("review payoff and stop/target calibration; profit factor is below 1")
    if pnls and wins / len(pnls) < 0.45:
        recommendations.append("audit entry filters and regime/session buckets before proposing an optimization")
    return {
        "source": source,
        "closed_trades": len(pnls),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / len(pnls) * 100, 2) if pnls else 0.0,
        "net_pnl": total,
        "average_pnl": avg,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss else None,
        "expectancy_positive": avg > 0,
        "verdict": "positive" if avg > 0 else ("insufficient_data" if not pnls else "negative"),
        "recommendations": recommendations,
        "auto_changes": [],
    }


def _software_review() -> dict[str, Any]:
    """AST-compile critical files and flag the known ledger namespace hazards."""
    compiled: list[str] = []
    syntax_errors: list[dict[str, str]] = []
    missing: list[str] = []
    for rel in _SOFTWARE_FILES:
        path = ROOT / rel
        if not path.exists():
            missing.append(rel)
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            compiled.append(rel)
        except (OSError, SyntaxError, UnicodeError) as exc:
            syntax_errors.append({"file": rel, "error": str(exc)})

    mode_doc = _safe_load("runtime_mode.json", {})
    mode = str(mode_doc.get("label") or "unknown") if isinstance(mode_doc, dict) else "unknown"
    mt5_orders = _safe_load("mt5_orders.json", {})
    paper_orders = _safe_load("paper_orders.json", {})
    namespace_ok = not (
        mode in {"growth", "live", "micro_live"}
        and isinstance(paper_orders, dict)
        and paper_orders.get("mode") == "mt5"
    )
    return {
        "files_checked": len(_SOFTWARE_FILES),
        "compiled": len(compiled),
        "missing": missing,
        "syntax_errors": syntax_errors,
        "runtime_profile": mode,
        "mt5_ledger_present": bool(isinstance(mt5_orders, dict) and mt5_orders.get("mode") == "mt5"),
        "paper_ledger_mode": paper_orders.get("mode") if isinstance(paper_orders, dict) else None,
        "namespace_consistent": namespace_ok,
        "verdict": "ok" if not missing and not syntax_errors and namespace_ok else "needs_attention",
    }


def _validate_instance(index: int, orders: list[dict[str, Any]], pipeline: dict[str, Any]) -> dict[str, Any]:
    """Validate one deterministic order/pipeline consistency sample."""
    if not orders:
        return {"ok": False, "reason": "no_orders_available"}
    order = orders[(index * 2654435761) % len(orders)]
    status = str(order.get("status") or "unknown")
    error = _order_error(order)
    ok = True
    reasons: list[str] = []
    if status in {"failed", "rejected"} and not error:
        ok = False
        reasons.append("failed_order_missing_error")
    if status in {"filled", "pending", "placed"} and error:
        ok = False
        reasons.append("successful_order_has_error")
    if status == "filled" and order.get("fill_price") is None and order.get("price") is None:
        ok = False
        reasons.append("filled_order_missing_price")
    if pipeline.get("kill_switch") and pipeline.get("approved_count", 0) > 0:
        ok = False
        reasons.append("approved_while_kill_switch_active")
    return {"ok": ok, "reason": ",".join(reasons) if reasons else "ok"}


def _run_instances(instance_count: int, orders: list[dict[str, Any]], pipeline: dict[str, Any]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for index in range(instance_count):
        result = _validate_instance(index, orders, pipeline)
        counts[result["reason"]] += 1
    failures = instance_count - counts.get("ok", 0)
    return {
        "kind": "deterministic_consistency_checks",
        "independent_simulations": False,
        "requested": instance_count,
        "completed": instance_count,
        "failed": failures,
        "passed": instance_count - failures,
        "failure_reasons": dict(counts),
        "pass_rate_pct": round((instance_count - failures) / instance_count * 100, 3) if instance_count else 0.0,
    }


def _run_tests(test_targets: list[str], timeout_seconds: int) -> dict[str, Any]:
    """Run an allow-listed test subset with its fixture state isolated."""
    safe_targets = [target for target in test_targets if target in _SAFE_TEST_TARGETS]
    if not safe_targets:
        safe_targets = list(_DEFAULT_TEST_TARGETS)
    command = [sys.executable, "-m", "pytest", *safe_targets, "-q", "--tb=short"]
    started = time.monotonic()
    try:
        # The test fixture honors this directory for active_profile cleanup.
        # Keeping it temporary prevents the recurring worker from touching the
        # live bot's state/active_profile.json while tests import the project.
        with tempfile.TemporaryDirectory(prefix="quant-validation-") as isolated_state:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1, timeout_seconds),
                check=False,
                env={
                    **os.environ,
                    "MT5_QUANT_VALIDATION": "1",
                    "MT5_QUANT_TEST_STATE_DIR": isolated_state,
                },
            )
        output = (completed.stdout + "\n" + completed.stderr).strip()
        return {
            "status": "passed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
            "targets": safe_targets,
            "summary": output[-2000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "returncode": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "targets": safe_targets,
            "summary": str(exc),
        }
    except OSError as exc:
        return {
            "status": "error",
            "returncode": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "targets": safe_targets,
            "summary": str(exc),
        }


def _input_doc(cfg: dict[str, Any]) -> dict[str, Any]:
    value = _safe_load("validation_input.json", {}) or {}
    return value if isinstance(value, dict) else {}


def run(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one offline validation cycle and persist only validation artifacts."""
    ensure_dirs()
    cfg = config or load_config()
    loop_cfg = cfg.get("validation_loop") or {}
    if not loop_cfg.get("enabled", True):
        return {"status": "disabled", "timestamp": utc_now_iso()}

    logger = setup_logger("validation_loop", "validation_loop.log")
    started = time.monotonic()
    instance_count = min(max(int(loop_cfg.get("instance_count", DEFAULT_INSTANCE_COUNT)), 1), 100_000)
    timeout = int(loop_cfg.get("test_timeout_seconds", DEFAULT_TEST_TIMEOUT_SECONDS))
    targets = list(loop_cfg.get("test_targets") or _DEFAULT_TEST_TARGETS)

    source, orders = _orders_for_review()
    approved = _safe_load("approved_signals.json", {})
    kill = _safe_load("kill_switch.json", {})
    approved = approved if isinstance(approved, dict) else {}
    kill = kill if isinstance(kill, dict) else {}
    pipeline = {
        "approved_count": int(approved.get("count", 0) or 0),
        "kill_switch": bool(kill.get("kill_switch")),
    }
    input_doc = _input_doc(cfg)
    test_result = _run_tests(targets, timeout)
    profit = _profit_review()
    software = _software_review()
    instances = _run_instances(instance_count, orders, pipeline)

    warnings: list[str] = []
    if source == "no_compatible_ledger":
        warnings.append("no compatible paper or MT5 ledger was available for validation")
    if pipeline["kill_switch"]:
        warnings.append("kill switch active; validation did not attempt execution")
    if profit["verdict"] == "negative":
        warnings.append("realized expectancy is negative; no profit change is auto-applied")
    if software["verdict"] != "ok":
        warnings.append("software review needs attention")
    if test_result["status"] != "passed":
        warnings.append(f"focused tests {test_result['status']}")

    result = {
        "timestamp": utc_now_iso(),
        "status": "attention" if warnings else "ok",
        "cycle_duration_seconds": round(time.monotonic() - started, 3),
        "safety": {
            "offline_only": True,
            "mt5_started": False,
            "orders_submitted": 0,
            "config_changed": False,
            "input": input_doc,
        },
        "tests": test_result,
        "profit_review": profit,
        "software_review": software,
        "instances": instances,
        "orders_source": source,
        "pipeline": pipeline,
        "warnings": warnings,
        "next_action": "manual_review" if warnings else "continue_monitoring",
        "auto_fix": {
            "enabled": False,
            "reason": "validation never edits source, config, or trading ledgers",
        },
    }
    write_json_state("validation_cycle.json", result)

    history = _safe_load("validation_history.json", [])
    if not isinstance(history, list):
        history = []
    history.append({
        "timestamp": result["timestamp"],
        "status": result["status"],
        "test_status": test_result["status"],
        "net_pnl": profit["net_pnl"],
        "expectancy_positive": profit["expectancy_positive"],
        "instances_failed": instances["failed"],
        "warnings": warnings,
    })
    write_json_state("validation_history.json", history[-_HISTORY_LIMIT:])
    logger.info(
        "Validation cycle: status=%s tests=%s instances=%d failed=%d net_pnl=%s warnings=%d duration=%.1fs",
        result["status"], test_result["status"], instance_count, instances["failed"], profit["net_pnl"], len(warnings), result["cycle_duration_seconds"],
    )
    return result


def watch(config: dict[str, Any] | None = None) -> None:
    """Run validation repeatedly; intended for a detached offline process."""
    cfg = config or load_config()
    interval = max(60, int((cfg.get("validation_loop") or {}).get("loop_interval_seconds", DEFAULT_INTERVAL_SECONDS)))
    logger = setup_logger("validation_loop", "validation_loop.log")
    while True:
        try:
            run(cfg)
        except Exception:
            logger.exception("Validation cycle crashed; retrying on next interval")
        time.sleep(interval)


if __name__ == "__main__":
    if "--watch" in sys.argv:
        watch()
    else:
        run()
