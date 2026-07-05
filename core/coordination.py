"""Team coordination checks — validate the trading OS is one coherent unit."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.campaign_symbols import ALL_CAMPAIGN_SYMBOLS
from core.setup_triggers import SETUP_ORDER
from core.utils import PROJECT_ROOT, load_config, read_json_state

STATE_DIR = PROJECT_ROOT / "state"

# Canonical pipeline from core/pipeline.py (adaptation_loop subsumes forward_test).
CANONICAL_LOOPS: tuple[str, ...] = (
    "data_loop",
    "feature_loop",
    "market_context_loop",
    "risk_loop",
    "signal_loop",
    "verifier_loop",
    "execution_loop",
    "blue_guardian_loop",
    "position_manager_loop",
    "memory_loop",
    "adaptation_loop",
    "trade_log_loop",
    "health_loop",
)

STALE_LOOPS: frozenset[str] = frozenset({"forward_test_loop"})

FRESH_STATE_FILES: tuple[str, ...] = (
    "supervisor.json",
    "health.json",
    "strategy_arena.json",
    "setup_catalog.json",
    "forward_test_ledger.json",
    "candidate_signals.json",
    "features.json",
    "market_context.json",
    "growth_campaign.json",
)

STATIC_STATE_FILES: tuple[str, ...] = (
    "runtime_mode.json",
)

MAX_STATE_AGE_SECONDS = 180.0
RUN_COUNT_TOLERANCE = 2


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(ts: str | None) -> float | None:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                text=True,
                errors="replace",
            )
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _count_dashboard_listeners(port: int = 8080) -> int:
    """Count distinct PIDs listening on a TCP port (Windows netstat)."""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"],
            text=True,
            errors="replace",
        )
    except Exception:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return 0
        except OSError:
            return 1
        finally:
            probe.close()
    needle = f":{port}"
    pids: set[str] = set()
    for line in out.splitlines():
        if "LISTENING" not in line or needle not in line:
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            pids.add(parts[-1])
    return len(pids)


def _supervisor_is_canonical(sup: dict[str, Any], lock_pid: int) -> bool:
    sup_pid = int(sup.get("agent_pid") or 0)
    if lock_pid and sup_pid == lock_pid:
        return True
    loop_names = {l.get("name") for l in (sup.get("loops") or []) if l.get("name")}
    return "adaptation_loop" in loop_names and not (loop_names & STALE_LOOPS)


def _read_trusted_supervisor(lock_pid: int, attempts: int = 5) -> dict[str, Any]:
    best: dict[str, Any] = {}
    for i in range(attempts):
        sup = read_json_state("supervisor.json", default={}) or {}
        if _supervisor_is_canonical(sup, lock_pid):
            return sup
        best = sup
        if i < attempts - 1:
            time.sleep(0.4)
    return best


def check_coordination(
    *,
    max_state_age_seconds: float = MAX_STATE_AGE_SECONDS,
) -> dict[str, Any]:
    """Return a structured coordination report. ``coordinated`` is True when all gates pass."""
    config = load_config()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, *, severity: str = "error") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "severity": severity})

    # --- Single instance ---
    lock = read_json_state("agent_lock.json", default={}) or {}
    lock_pid = int(lock.get("pid") or 0)
    lock_alive = _pid_alive(lock_pid)
    dash_count = _count_dashboard_listeners(8080)
    add(
        "canonical_agent_alive",
        lock_alive,
        f"agent_lock pid={lock_pid} alive={lock_alive}",
    )
    add(
        "single_bot_instance",
        dash_count <= 1,
        f"dashboard listeners on :8080 = {dash_count} (want ≤1)",
        severity="warning",
    )

    # --- Supervisor ---
    sup = _read_trusted_supervisor(lock_pid)
    sup_pid = int(sup.get("agent_pid") or 0)
    if lock_pid and sup_pid and sup_pid != lock_pid:
        add(
            "supervisor_owner",
            False,
            f"supervisor agent_pid={sup_pid} != lock pid={lock_pid}",
        )
    else:
        add(
            "supervisor_owner",
            True,
            f"agent_pid={sup_pid or 'legacy'} lock={lock_pid}",
        )
    add(
        "supervisor_healthy",
        sup.get("overall_status") == "healthy",
        f"overall_status={sup.get('overall_status')!r}",
    )

    trading = next(
        (s for s in (sup.get("services") or []) if s.get("name") == "trading_pipeline"),
        {},
    )
    pipeline_status = trading.get("status")
    add(
        "trading_pipeline_ok",
        pipeline_status in ("ok", "running") and int(trading.get("error_count") or 0) == 0,
        f"status={pipeline_status} errors={trading.get('error_count')}",
    )

    loop_map = {l.get("name"): l for l in (sup.get("loops") or []) if l.get("name")}
    stale = [n for n in loop_map if n in STALE_LOOPS]
    add(
        "no_stale_loops",
        not stale,
        f"stale loop names in supervisor: {stale or 'none'}",
    )

    missing = [n for n in CANONICAL_LOOPS if n not in loop_map]
    add(
        "canonical_loops_present",
        not missing,
        f"missing: {missing or 'none'}",
    )

    bad_status = [
        n for n in CANONICAL_LOOPS
        if loop_map.get(n, {}).get("status") != "ok"
    ]
    add(
        "all_loops_ok",
        not bad_status,
        f"non-ok: {bad_status or 'none'}",
    )

    run_counts = [
        int(loop_map[n].get("run_count") or 0)
        for n in CANONICAL_LOOPS
        if n in loop_map
    ]
    if run_counts:
        spread = max(run_counts) - min(run_counts)
        add(
            "loop_run_counts_aligned",
            spread <= RUN_COUNT_TOLERANCE,
            f"run_count spread={spread} (max {RUN_COUNT_TOLERANCE})",
        )

    # --- Runtime mode ---
    mode = read_json_state("runtime_mode.json", default={}) or {}
    add("growth_mode", mode.get("label") == "growth", f"label={mode.get('label')!r}")
    add("arena_active", bool(mode.get("arena_active")), f"arena_active={mode.get('arena_active')}")
    add(
        "growth_plan_active",
        bool(mode.get("growth_plan_active")),
        f"growth_plan_active={mode.get('growth_plan_active')}",
    )

    # --- Symbols ---
    arena = read_json_state("strategy_arena.json", default={}) or {}
    arena_syms = list(arena.get("symbols") or [])
    expected = list(ALL_CAMPAIGN_SYMBOLS)
    add(
        "arena_14_symbols",
        len(arena_syms) == 14 and set(arena_syms) == set(expected),
        f"arena symbols={len(arena_syms)}",
    )

    mt5_syms = list(config.get("mt5", {}).get("symbols") or [])
    add(
        "config_14_symbols",
        len(mt5_syms) == 14 and set(mt5_syms) == set(expected),
        f"mt5.symbols={len(mt5_syms)}",
    )

    setups = list(arena.get("setup_types") or [])
    add(
        "arena_8_setups",
        len(setups) == 8 and set(setups) == set(SETUP_ORDER),
        f"setups={len(setups)}",
    )

    campaign = arena.get("campaign_id") or ""
    add(
        "arena_campaign",
        bool(campaign),
        f"campaign_id={campaign!r}",
    )

    catalog = read_json_state("setup_catalog.json", default={}) or {}
    add(
        "setup_catalog",
        int(catalog.get("setup_count") or 0) == 8,
        f"setup_count={catalog.get('setup_count')}",
    )

    # --- State freshness ---
    stale_files: list[str] = []
    missing_static: list[str] = []
    for fname in FRESH_STATE_FILES:
        path = STATE_DIR / fname
        if not path.exists():
            stale_files.append(f"{fname}(missing)")
            continue
        age = (datetime.now(timezone.utc) - datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        )).total_seconds()
        if age > max_state_age_seconds:
            stale_files.append(f"{fname}({int(age)}s)")
    for fname in STATIC_STATE_FILES:
        if not (STATE_DIR / fname).exists():
            missing_static.append(fname)
    add(
        "state_files_fresh",
        not stale_files and not missing_static,
        stale_files and f"stale: {', '.join(stale_files)}"
        or missing_static and f"missing: {', '.join(missing_static)}"
        or "all fresh",
    )

    # --- MT5 / health ---
    health = read_json_state("health.json", default={}) or {}
    conn = health.get("connection") or {}
    add(
        "mt5_connected",
        bool(conn.get("alive")) and bool(conn.get("logged_in")),
        f"alive={conn.get('alive')} logged_in={conn.get('logged_in')}",
    )
    add(
        "health_ok",
        health.get("status") == "healthy",
        f"status={health.get('status')!r} issues={len(health.get('issues') or [])}",
    )

    # --- Cross-loop wiring ---
    ledger = read_json_state("forward_test_ledger.json", default={}) or {}
    ledger_age = _age_seconds(ledger.get("updated_at"))
    add(
        "adaptation_ledger",
        ledger_age is not None and ledger_age <= max_state_age_seconds,
        f"ledger age={int(ledger_age) if ledger_age is not None else 'unknown'}s",
    )

    arena_settings = config.get("strategy_arena") or {}
    mop = int(arena_settings.get("max_open_per_symbol") or 0)
    add(
        "arena_max_open",
        mop == 5,
        f"max_open_per_symbol={mop} (want 5)",
    )

    from core.learning_health import assess_trade_learning

    learning = assess_trade_learning()
    archive = read_json_state("signal_archive.json", default={}) or {}
    add(
        "signal_archive",
        int(archive.get("count") or 0) > 0 or learning["total_trades"] == 0,
        f"archived_signals={archive.get('count', 0)}",
        severity="warning",
    )
    culturing_ok = learning.get("culturing_ready_pct", 0) >= 30.0 or learning.get("culturing_cells", 0) >= 8
    add(
        "learning_pipeline",
        learning["total_trades"] == 0 or culturing_ok,
        learning.get("detail", ""),
        severity="warning",
    )

    errors = [c for c in checks if not c["ok"] and c["severity"] == "error"]
    warnings = [c for c in checks if not c["ok"] and c["severity"] == "warning"]
    coordinated = len(errors) == 0

    return {
        "coordinated": coordinated,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "pipeline_loops": list(CANONICAL_LOOPS),
        "supervisor_run_count": trading.get("run_count"),
    }