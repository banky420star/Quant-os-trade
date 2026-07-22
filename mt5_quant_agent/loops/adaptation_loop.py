"""Adaptation loop — bot develops as it trades when conditions are met.

Runs AFTER execution + memory on each pipeline cycle. When new closes are
detected:
  1. Rebuild culturing ledger + per-symbol vetoes (forward_test)
  2. Recalibrate BE/trailing per symbol when enough MAE/MFE data exists
  3. Log what changed (vetoes, trail params, edge shifts) to adaptation_log.json

The verifier, ranker, Kelly sizer, and position manager read the updated state
on the next signal cycle — so behavior evolves from live conditions, not static
config alone.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.adaptation_engine import (  # noqa: E402
    compile_adaptation_report,
    diff_be_trail,
    diff_edge_stats,
    diff_vetoes,
)
from core.positive_evolution import (  # noqa: E402
    diff_positive_cells,
    positive_evolution_active,
    refresh_positive_evolution,
)
from core.regime_evolution import (  # noqa: E402
    regime_evolution_enabled,
    refresh_regime_evolution,
)
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state  # noqa: E402


def _adaptation_cfg(config: dict[str, Any]) -> dict[str, Any]:
    cfg = config.get("adaptation") or {}
    revo = cfg.get("regime_evolution") or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "calibrate_be_trail": bool(cfg.get("calibrate_be_trail", True)),
        "rebuild_trade_log": bool(cfg.get("rebuild_trade_log", True)),
        "refresh_positive_evolution": bool(cfg.get("refresh_positive_evolution", True)),
        "refresh_regime_evolution": bool(
            revo.get("enabled", True) if isinstance(revo, dict) else True
        ),
        "auto_evolve_cells": bool(cfg.get("auto_evolve_cells", False)),
    }


def run() -> dict[str, Any]:
    config = load_config()
    acfg = _adaptation_cfg(config)
    logger = setup_logger("adaptation_loop", "adaptation_loop.log")

    if not acfg["enabled"]:
        logger.debug("Adaptation disabled in config")
        return read_json_state("adaptation_log.json", default={}) or {}

    state = read_json_state("adaptation_state.json", default={}) or {}
    memory = read_json_state("memory.json", default={"records": [], "total_records": 0}) or {}
    memory_count = int(memory.get("total_records") or len(memory.get("records") or []))
    prev_memory_count = state.get("memory_count")
    if prev_memory_count is None and memory_count > 0:
        # First boot — baseline existing history; only adapt on future closes.
        prev_memory_count = memory_count
    prev_memory_count = int(prev_memory_count or 0)
    new_closes = max(0, memory_count - prev_memory_count)

    old_policy = read_json_state("symbol_policy_live.json", default={}) or {}
    old_be_trail = read_json_state("symbol_be_trail_live.json", default={}) or {}
    old_edge = read_json_state("edge_scores.json", default={}) or {}
    old_positive = read_json_state("positive_evolution.json", default={}) or {}
    old_setup_stats = old_edge.get("setup_stats") or {}

    # Culturing ledger + vetoes refresh every cycle (same as legacy forward_test_loop).
    ledger_payload: dict[str, Any] = {}
    try:
        from loops.forward_test_loop import run as forward_test_run
        ledger_payload = forward_test_run() or {}
    except Exception as exc:  # noqa: BLE001
        logger.error("Forward-test adaptation failed: %s", exc)

    # Regime-conditioned progression: promote/demote symbol|regime|setup cells
    # from closed trades so verify/eval apply different gates per market state.
    if acfg["refresh_regime_evolution"] and regime_evolution_enabled(config):
        try:
            revo = refresh_regime_evolution(config)
            logger.info(
                "Regime evolution: cells=%s promoted=%s demoted=%s",
                revo.get("n_cells"),
                revo.get("n_promoted"),
                revo.get("n_demoted"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Regime evolution refresh failed: %s", exc)

    # Session-edge evolution: auto-promote winning cells, keep culturing vetoes.
    if acfg["auto_evolve_cells"]:
        try:
            from quant.research.adaptation_evolution import evolve_symbol_policy
            evolved = evolve_symbol_policy(ledger_payload, config, existing_policy=old_policy)
            if evolved.get("symbols"):
                write_json_state("symbol_policy_live.json", evolved)
                logger.info(
                    "Cell evolution: %d symbols, positive=%s",
                    len(evolved.get("symbols") or {}),
                    (evolved.get("evolution") or {}).get("positive_evolution"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cell evolution skipped: %s", exc)

    if new_closes == 0:
        write_json_state("adaptation_state.json", {
            "updated_at": utc_now_iso(),
            "memory_count": memory_count,
            "policy_snapshot": read_json_state("symbol_policy_live.json", default={}) or {},
            "be_trail_snapshot": read_json_state("symbol_be_trail_live.json", default={}) or {},
        })
        return read_json_state("adaptation_log.json", default={}) or {}

    logger.info("Adaptation triggered: %d new close(s) since last cycle", new_closes)
    new_records = (memory.get("records") or [])[-new_closes:]

    if acfg["rebuild_trade_log"]:
        try:
            from loops.trade_log_loop import run as trade_log_run
            trade_log_run()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Trade log rebuild during adaptation failed: %s", exc)

    if acfg["calibrate_be_trail"]:
        try:
            import yaml
            from scripts.calibrate_be_trail import calibrate
            with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            out = calibrate(cfg, logger)
            out["updated_at"] = utc_now_iso()
            write_json_state("symbol_be_trail_live.json", out)
            logger.info(
                "BE/trail calibration: %d symbols, %d trusted",
                len(out.get("symbols") or {}),
                sum(1 for v in (out.get("symbols") or {}).values() if v.get("trusted")),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("BE/trail calibration skipped: %s", exc)

    new_policy = read_json_state("symbol_policy_live.json", default={}) or {}
    new_be_trail = read_json_state("symbol_be_trail_live.json", default={}) or {}
    new_edge = read_json_state("edge_scores.json", default={}) or {}
    ledger_cells = (ledger_payload.get("cells") or {}) if isinstance(ledger_payload, dict) else {}

    positive_diff: dict[str, list[dict[str, Any]]] = {"added": [], "removed": []}
    if acfg["refresh_positive_evolution"] and positive_evolution_active(config):
        try:
            new_positive = refresh_positive_evolution(config, ledger_cells=ledger_cells, logger=logger)
            positive_diff = diff_positive_cells(old_positive, new_positive)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Positive evolution refresh failed: %s", exc)

    report = compile_adaptation_report(
        new_trade_count=new_closes,
        new_records=new_records,
        veto_diff=diff_vetoes(old_policy, new_policy, ledger_cells),
        be_trail_changes=diff_be_trail(old_be_trail, new_be_trail),
        edge_shifts=diff_edge_stats(old_setup_stats, new_edge.get("setup_stats") or {}),
        positive_diff=positive_diff,
    )

    log_doc = read_json_state("adaptation_log.json", default={"history": []}) or {}
    history = list(log_doc.get("history") or [])
    history.append(report)
    history = history[-200:]
    log_doc = {
        "updated_at": utc_now_iso(),
        "last": report,
        "history": history,
        "total_cycles": int(log_doc.get("total_cycles") or 0) + 1,
    }
    write_json_state("adaptation_log.json", log_doc)

    write_json_state("adaptation_state.json", {
        "updated_at": utc_now_iso(),
        "memory_count": memory_count,
        "policy_snapshot": new_policy,
        "be_trail_snapshot": new_be_trail,
    })

    logger.info("Adaptation: %s", report.get("summary"))
    if report.get("veto_added"):
        for v in report["veto_added"][:5]:
            logger.info(
                "  VETO + %s | %s | n=%s wr=%s%% exp=%sR",
                v.get("symbol"), v.get("cell"), v.get("n"),
                v.get("win_rate_pct"), v.get("expectancy_net_r"),
            )
    return log_doc