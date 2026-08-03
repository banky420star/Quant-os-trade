"""Hourly specialized setup observer.

This loop is deliberately analytical: it reads the feature/context/evidence
snapshots produced by the normal pipeline, classifies setups with the existing
SetupClassifier, and writes a bounded report. It never initializes MT5 and
never calls a broker or execution API.
"""

from __future__ import annotations

import logging
from typing import Any

from core.setup_classifier import SetupClassifier
from core.utils import (
    fail_safe_missing,
    load_config,
    read_json_state,
    setup_logger,
    utc_now_iso,
    write_json_state,
)

REPORT_FILE = "specialized_setup_report.json"
MAX_SYMBOLS_DEFAULT = 20
MAX_SETUPS_PER_SYMBOL = 8


def _empty_report(status: str, reason: str, *, symbols_scanned: int = 0) -> dict[str, Any]:
    return {
        "timestamp": utc_now_iso(),
        "status": status,
        "status_reason": reason,
        "safety": {
            "mode": "observe_only",
            "orders_submitted": 0,
            "mt5_started": False,
            "execution_called": False,
        },
        "symbols_scanned": symbols_scanned,
        "observations": {},
    }


def _symbol_observation(
    symbol: str,
    feat: dict[str, Any],
    context: dict[str, Any],
    evidence: dict[str, Any],
    classifier: SetupClassifier,
) -> dict[str, Any]:
    """Build a bounded, dashboard-friendly observation for one symbol."""
    setups = classifier.classify_all(feat, context, evidence)
    bounded = []
    for setup in setups[:MAX_SETUPS_PER_SYMBOL]:
        bounded.append({
            "setup_type": setup.get("setup_type"),
            "side": setup.get("side"),
            "setup_confidence": setup.get("setup_confidence"),
            "reason": setup.get("reason"),
        })
    top = bounded[0] if bounded else None
    market_regime = context.get("market_regime")
    if not isinstance(market_regime, dict):
        market_regime = {}
    return {
        "symbol": symbol,
        "price": feat.get("price"),
        "market_regime": market_regime.get("primary"),
        "regime": context.get("regime"),
        "phase": context.get("phase"),
        "move_type": context.get("move_type"),
        "session": context.get("session"),
        "evidence": {
            key: evidence.get(key)
            for key in ("trend", "momentum", "structure", "liquidity", "volatility", "volume", "risk")
            if key in evidence
        },
        "setup_count": len(setups),
        "top_setup": top,
        "setups": bounded,
    }


def run(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Scan the latest pipeline snapshots and persist an observe-only report."""
    cfg = config or load_config()
    logger = setup_logger("specialized_setup_loop", "specialized_setup_loop.log")
    loop_cfg = cfg.get("specialized_setup_loop") or {}
    try:
        requested_max_symbols = int(loop_cfg.get("max_symbols_to_scan", MAX_SYMBOLS_DEFAULT))
    except (TypeError, ValueError):
        requested_max_symbols = MAX_SYMBOLS_DEFAULT
    max_symbols = max(1, min(requested_max_symbols, 100))

    if not loop_cfg.get("enabled", True):
        report = _empty_report("disabled", "disabled_by_configuration")
        write_json_state(REPORT_FILE, report)
        return {"specialized_setup_loop": "OK", "status": "disabled"}

    if fail_safe_missing("features.json", logger):
        report = _empty_report("waiting", "features.json_missing")
        write_json_state(REPORT_FILE, report)
        return {"specialized_setup_loop": "OK", "status": "waiting"}

    features = read_json_state("features.json", default={})
    context_doc = read_json_state("market_context.json", default={})
    if not isinstance(features, dict):
        report = _empty_report("waiting", "features.json_invalid_shape")
        write_json_state(REPORT_FILE, report)
        return {"specialized_setup_loop": "OK", "status": "waiting"}
    if not isinstance(context_doc, dict):
        context_doc = {}
    feature_symbols = features.get("symbols") or {}
    market_context_doc = context_doc.get("market_context")
    evidence_doc = context_doc.get("evidence")
    context_symbols = (
        market_context_doc.get("symbols", {})
        if isinstance(market_context_doc, dict)
        else {}
    ) or {}
    evidence_symbols = (
        evidence_doc.get("symbols", {})
        if isinstance(evidence_doc, dict)
        else {}
    ) or {}
    if not isinstance(context_symbols, dict):
        context_symbols = {}
    if not isinstance(evidence_symbols, dict):
        evidence_symbols = {}

    if not isinstance(feature_symbols, dict) or not feature_symbols:
        report = _empty_report("waiting", "features.json_has_no_symbols")
        write_json_state(REPORT_FILE, report)
        return {"specialized_setup_loop": "OK", "status": "waiting"}

    classifier = SetupClassifier(cfg, logger)
    observations: dict[str, Any] = {}
    for symbol, feat in list(feature_symbols.items())[:max_symbols]:
        if not isinstance(feat, dict):
            logger.warning("Skipping %s: feature row is not an object", symbol)
            continue
        context = context_symbols.get(symbol, {})
        evidence = evidence_symbols.get(symbol, {})
        if not isinstance(context, dict):
            context = {}
        if not isinstance(evidence, dict):
            evidence = {}
        try:
            observations[symbol] = _symbol_observation(
                symbol, feat, context, evidence, classifier,
            )
        except Exception as exc:  # isolate one malformed symbol from the scan
            logger.warning("Setup scan failed for %s: %s", symbol, exc)
            observations[symbol] = {
                "symbol": symbol,
                "setup_count": 0,
                "top_setup": None,
                "error": str(exc),
            }

    report = {
        "timestamp": utc_now_iso(),
        "status": "ok",
        "status_reason": "scan_complete",
        "input_timestamps": {
            "features": features.get("timestamp"),
            "market_context": context_doc.get("timestamp"),
        },
        "safety": {
            "mode": "observe_only",
            "orders_submitted": 0,
            "mt5_started": False,
            "execution_called": False,
        },
        "symbols_scanned": len(observations),
        "symbols_available": len(feature_symbols),
        "observations": observations,
    }
    write_json_state(REPORT_FILE, report)
    logger.info(
        "Specialized setup scan complete: %d/%d symbols, observe_only=True",
        len(observations), len(feature_symbols),
    )
    return {
        "specialized_setup_loop": "OK",
        "status": "ok",
        "symbols_scanned": len(observations),
        "setups_found": sum(int(row.get("setup_count", 0)) for row in observations.values()),
    }


if __name__ == "__main__":
    run()
