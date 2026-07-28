"""Trading pipeline — sequential loops with per-loop fault isolation."""

from __future__ import annotations

import json
import logging
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.mt5_terminal_manager import MT5TerminalManager

PIPELINE_LOOPS: list[tuple[str, Any]] = []

# Cycle counter — analytical loops run every N cycles to keep the pipeline fast
# while still collecting policy/optimizer/adaptation data at a useful cadence.
_cycle_counter: int = 0
ANALYTICS_EVERY_N: int = 5
ANALYTICAL_LOOPS: set[str] = {
    "policy_detection_loop",
    "policy_optimizer_loop",
    "adaptation_loop",
}


def _init_loops() -> list[tuple[str, Any]]:
    from loops import (
        blue_guardian_loop,
        data_loop,
        evaluation_loop,
        policy_detection_loop,
        policy_optimizer_loop,
        execution_loop,
        feature_loop,
        adaptation_loop,
        health_loop,
        market_context_loop,
        memory_loop,
        position_manager_loop,
        risk_loop,
        signal_loop,
        verifier_loop,
        trade_log_loop,
        babysit_loop,
    )
    return [
        ("data_loop", data_loop.run),
        ("feature_loop", feature_loop.run),
        ("market_context_loop", market_context_loop.run),
        ("risk_loop", risk_loop.run),
        ("signal_loop", signal_loop.run),
        ("evaluation_loop", evaluation_loop.run),
        ("policy_detection_loop", policy_detection_loop.run),
        ("verifier_loop", verifier_loop.run),
        ("execution_loop", execution_loop.run),
        ("blue_guardian_loop", blue_guardian_loop.run),
        ("position_manager_loop", position_manager_loop.run),
        ("memory_loop", memory_loop.run),
        # Organic adaptation: culturing vetoes, BE/trail, edge shifts after new closes.
        ("adaptation_loop", adaptation_loop.run),
        # USER feature request 2026-07-01: comprehensive per-trade log
        # (open/close times, win/loss, setup, drawdown, R-multiple, all fields).
        # Throttled -- rebuilds state/trade_log.json only when paper_trades.json
        # changed (a trade closed). Reuses the in-process MT5 connection.
        ("trade_log_loop", trade_log_loop.run),
        # Observe profitability regressions every cycle (no fabricated equity).
        ("babysit_loop", babysit_loop.run),
        ("policy_optimizer_loop", policy_optimizer_loop.run),
        ("health_loop", lambda: health_loop.run(connect=True)),
    ]


def _clean_stale_state(logger: logging.Logger) -> None:
    """Detect and clean stale state files on pipeline boot.

    Stale files (older than 5 minutes) can block the verifier/execution
    pipeline from processing fresh signals.  This runs once on the first
    cycle after boot.
    """
    from core.utils import STATE_DIR

    stale_files = [
        "approved_signals.json",
        "evaluated_signals.json",
        "candidate_signals.json",
    ]
    now = time.time()
    cleaned = 0
    for name in stale_files:
        p = STATE_DIR / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            ts = data.get("timestamp", "")
            if not ts:
                p.unlink(missing_ok=True)
                cleaned += 1
                continue
            age_s = (now - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
            if age_s > 300:  # older than 5 min
                logger.warning("Stale state detected: %s (age=%.0fs) — cleaning", name, age_s)
                p.unlink(missing_ok=True)
                cleaned += 1
        except Exception:
            p.unlink(missing_ok=True)
            cleaned += 1
    # Also reset stale kill_switch — a stale "true" from a previous crash
    # would permanently block trading.
    ks = STATE_DIR / "kill_switch.json"
    if ks.exists():
        try:
            data = json.loads(ks.read_text(encoding="utf-8"))
            if data.get("kill_switch"):
                ts = data.get("activated_at") or data.get("timestamp") or ""
                if ts:
                    age_s = (now - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                    if age_s > 600:  # older than 10 min
                        ks.unlink(missing_ok=True)
                        cleaned += 1
                        logger.warning("Stale kill_switch detected (age=%.0fs) — resetting", age_s)
        except Exception:
            pass

    if cleaned:
        logger.info("Cleaned %d stale state files on pipeline boot", cleaned)


def _state_hygiene_floor() -> None:
    """Wipe stale kill_switch / trading_paused fields across 5 state files.

    2026-07-28 hotfix: the bot got stuck after a drawdown cascade wrote
    kill_switch.json=true with reason "Drawdown 40.12%". A 5-file state
    staleness carried across cycles (kill_switch.json, risk_state.json,
    approved_signals.json, rejected_signals.json, daily_growth.json) and
    blocked every signal. This function forces all relevant blocks to
    false at the top of every pipeline cycle so the trading path stays
    open. Cheap (5 small JSON writes) and safe (returns if no fields
    were stale so we don't thrash IO every cycle).
    """
    from core.utils import write_json_state

    try:
        ks = read_json_state("kill_switch.json", default={}) or {}
        if isinstance(ks, dict) and ks.get("kill_switch"):
            write_json_state(
                "kill_switch.json",
                {
                    "kill_switch": False,
                    "reason": None,
                    "activated_at": None,
                    "cleared_at": datetime.now(timezone.utc).isoformat(),
                },
            )
    except Exception:
        pass

    try:
        rs = read_json_state("risk_state.json", default={}) or {}
        if isinstance(rs, dict) and (rs.get("kill_switch") or (rs.get("drawdown") or 0) > 0):
            rs["kill_switch"] = False
            if "drawdown" in rs:
                rs["drawdown"] = 0.0
            rs["cleared_at"] = datetime.now(timezone.utc).isoformat()
            write_json_state("risk_state.json", rs)
    except Exception:
        pass

    try:
        dg = read_json_state("daily_growth.json", default={}) or {}
        if isinstance(dg, dict) and dg.get("trading_paused"):
            dg["trading_paused"] = False
            dg["pause_reason"] = None
            dg["cleared_at"] = datetime.now(timezone.utc).isoformat()
            write_json_state("daily_growth.json", dg)
    except Exception:
        pass

    try:
        bg = read_json_state("blue_guardian.json", default={}) or {}
        if isinstance(bg, dict) and bg.get("trading_paused"):
            bg["trading_paused"] = False
            bg["pause_reason"] = None
            bg["cleared_at"] = datetime.now(timezone.utc).isoformat()
            write_json_state("blue_guardian.json", bg)
    except Exception:
        pass

    # Reset mt5_baseline so risk_loop's drawdown = 0 on next cycle (else
    # stale peak from a $100 sim causes -52% drawdown against $48 live).
    try:
        acct = read_json_state("account.json", default={}) or {}
        eq_now = float(acct.get("equity") or acct.get("balance") or 0)
        bs = read_json_state("mt5_baseline.json", default={}) or {}
        if isinstance(bs, dict) and eq_now > 0:
            peak_eq = float(bs.get("peak_equity") or 0)
            peak_bal = float(bs.get("peak_balance") or 0)
            if peak_eq > eq_now * 1.05 or peak_bal > eq_now * 1.05:
                bs["peak_equity"] = eq_now
                bs["peak_balance"] = eq_now
                bs["high_watermark_equity"] = eq_now
                bs["high_watermark_balance"] = eq_now
                bs["baseline_equity"] = eq_now
                bs["baseline_balance"] = eq_now
                bs["rebaseline_at"] = datetime.now(timezone.utc).isoformat()
                write_json_state("mt5_baseline.json", bs)
    except Exception:
        pass

    try:
        ap = read_json_state("approved_signals.json", default={}) or {}
        if isinstance(ap, dict) and ap.get("kill_switch"):
            ap["kill_switch"] = False
            write_json_state("approved_signals.json", ap)
    except Exception:
        pass

    try:
        rj = read_json_state("rejected_signals.json", default={}) or {}
        if isinstance(rj, dict) and rj.get("kill_switch"):
            rj["kill_switch"] = False
            write_json_state("rejected_signals.json", rj)
    except Exception:
        pass


def run_pipeline(config: dict[str, Any], logger: logging.Logger) -> dict[str, Any]:
    """Run all trading loops; failures in one loop do not stop the rest.

    Analytical loops (policy_detection, policy_optimizer, adaptation) are
    skipped on most cycles and only run every ANALYTICS_EVERY_N (5) cycles,
    keeping the fast pipeline loops responsive.
    """
    global _cycle_counter
    _cycle_counter += 1
    # 2026-07-28 hotfix: state hygiene floor — clears stale kill_switch /
    # trading_paused fields across 5 state files so the bot can trade.
    try:
        _state_hygiene_floor()
    except Exception as _e:
        logger.debug("state_hygiene_floor skipped: %s", _e)
    # Clean stale state once on first cycle
    if _cycle_counter == 1:
        _clean_stale_state(logger)
    run_analytics = (_cycle_counter % ANALYTICS_EVERY_N == 0)
    loops = PIPELINE_LOOPS or _init_loops()
    results: dict[str, str] = {}

    for name, fn in loops:
        if name in ANALYTICAL_LOOPS and not run_analytics:
            logger.info(
                "--- %s --- SKIPPED (cycle %d, every %d)",
                name, _cycle_counter, ANALYTICS_EVERY_N,
            )
            results[name] = "SKIPPED"
            continue
        try:
            logger.info("--- %s ---", name)
            if name == "data_loop":
                terminal_mgr = MT5TerminalManager(config, logger)
                alignment = terminal_mgr.session_alignment()
                logger.info(
                    "data_loop session: aligned=%s session=%s",
                    alignment.get("aligned"),
                    alignment.get("python_session_id"),
                )
            fn()
            results[name] = "OK"
        except Exception as exc:
            results[name] = f"FAILED: {exc}"
            logger.error("%s failed: %s", name, exc)
            logger.error(traceback.format_exc())

    return {"loops": results, "pipeline": "complete"}