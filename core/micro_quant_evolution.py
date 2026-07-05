"""Micro-account adaptive weight evolution — replay-validated per-symbol updates."""

from __future__ import annotations

import copy
import logging
from typing import Any

from core.adaptive_weights import DEFAULT_WEIGHTS
from core.edge_database import EdgeDatabase
from core.replay_engine import ReplayEngine
from core.utils import read_json_state, utc_now_iso, write_json_state
from core.weight_defaults import SUBSYSTEM_WEIGHTS

MICRO_SYMBOLS = ("XAUUSDm", "USOILm", "UK100m")
STATE_CANDIDATES = "weight_candidates.json"
STATE_EVOLUTION_LOG = "micro_evolution_log.json"


def micro_evolution_settings(config: dict[str, Any]) -> dict[str, Any]:
    quant = config.get("quant") or {}
    raw = quant.get("micro_evolution") or {}
    symbols = list(raw.get("symbols") or MICRO_SYMBOLS)
    return {
        "enabled": bool(raw.get("enabled", False)),
        "symbols": symbols,
        "min_trades_per_symbol": int(raw.get("min_trades_per_symbol", 8)),
        "improvement_threshold_pct": float(raw.get("improvement_threshold_pct", 3.0)),
        "replay_bars": int(raw.get("replay_bars", 300)),
        "replay_step": int(raw.get("replay_step", 15)),
        "require_positive_expectancy": bool(raw.get("require_positive_expectancy", True)),
    }


def micro_evolution_enabled(config: dict[str, Any]) -> bool:
    settings = micro_evolution_settings(config)
    if settings["enabled"]:
        return True
    profile = str(config.get("active_profile") or "").lower()
    return profile in ("30-c2", "30_c2")


def replay_expectancy_usd(replay_out: dict[str, Any]) -> float:
    trades = int(replay_out.get("trades_closed", 0))
    if trades <= 0:
        return 0.0
    return round(float(replay_out.get("pnl_total", 0)) / trades, 4)


def replay_fitness_score(replay_out: dict[str, Any]) -> float:
    pnl = float(replay_out.get("pnl_total", 0))
    wr = float(replay_out.get("win_rate_pct", 0))
    trades = int(replay_out.get("trades_closed", 0))
    exp = replay_expectancy_usd(replay_out)
    return pnl + exp * 10.0 + (wr - 50.0) * 0.3 + min(trades, 30) * 0.05


def _normalize_weights(raw: dict[str, float]) -> dict[str, float]:
    total = sum(raw.values()) or 1.0
    return {k: round(v / total, 4) for k, v in raw.items()}


def optimize_symbol_weights(
    records: list[dict[str, Any]],
    symbol: str,
    *,
    min_trades: int = 8,
) -> dict[str, Any]:
    sym_records = [r for r in records if r.get("symbol") == symbol]
    if len(sym_records) < min_trades:
        return {
            "symbol": symbol,
            "status": "insufficient_data",
            "trade_count": len(sym_records),
            "min_trades": min_trades,
            "weights": dict(DEFAULT_WEIGHTS),
        }

    engines = list(SUBSYSTEM_WEIGHTS.keys())
    win_scores = {e: 0.0 for e in engines}
    loss_scores = {e: 0.0 for e in engines}
    win_n = loss_n = 0

    for rec in sym_records:
        tree = rec.get("confidence_tree") or {}
        if not tree:
            continue
        if rec.get("result") == "win":
            win_n += 1
            for e in engines:
                win_scores[e] += float(tree.get(e, 50))
        elif rec.get("result") == "loss":
            loss_n += 1
            for e in engines:
                loss_scores[e] += float(tree.get(e, 50))

    raw: dict[str, float] = {}
    for e in engines:
        win_avg = (win_scores[e] / win_n) if win_n else 50.0
        loss_avg = (loss_scores[e] / loss_n) if loss_n else 50.0
        edge = max(0.0, win_avg - loss_avg * 0.5)
        raw[e] = edge + 10.0

    new_weights = _normalize_weights(raw)
    baseline_total = sum(SUBSYSTEM_WEIGHTS.values())
    normalized_baseline = {
        e: round(SUBSYSTEM_WEIGHTS[e] / baseline_total, 4) for e in engines
    }
    deltas = {
        e: round((new_weights[e] - normalized_baseline[e]) * 100, 2) for e in engines
    }

    return {
        "symbol": symbol,
        "status": "candidate",
        "trade_count": len(sym_records),
        "wins_analyzed": win_n,
        "losses_analyzed": loss_n,
        "weights": new_weights,
        "baseline_weights": normalized_baseline,
        "deltas_pct": deltas,
        "method": "micro_symbol_win_loss_correlation",
    }


def _improvement_pct(baseline_score: float, candidate_score: float) -> float:
    if abs(baseline_score) > 0.01:
        return (candidate_score - baseline_score) / abs(baseline_score) * 100.0
    if candidate_score > baseline_score:
        return 100.0
    return 0.0


def _replay_symbol(
    config: dict[str, Any],
    symbol: str,
    weights: dict[str, float] | None,
    *,
    replay_bars: int,
    replay_step: int,
    logger: logging.Logger,
) -> dict[str, Any]:
    cfg = copy.deepcopy(config)
    if weights:
        cfg["optimizer_weights"] = weights
    engine = ReplayEngine(cfg, logger)
    return engine.run(symbol=symbol, max_bars=replay_bars, step=replay_step)


def _merge_global_weights(per_symbol: dict[str, dict[str, Any]]) -> dict[str, float]:
    engines = list(SUBSYSTEM_WEIGHTS.keys())
    totals = {e: 0.0 for e in engines}
    weight_mass = 0.0
    for sym_doc in per_symbol.values():
        if sym_doc.get("proposal") != "deploy":
            continue
        w = sym_doc.get("weights") or {}
        mass = max(1, int(sym_doc.get("trade_count", 1)))
        weight_mass += mass
        for e in engines:
            totals[e] += float(w.get(e, DEFAULT_WEIGHTS.get(e, 0))) * mass
    if weight_mass <= 0:
        return dict(DEFAULT_WEIGHTS)
    return _normalize_weights({e: totals[e] / weight_mass for e in engines})


def run_micro_evolution(
    config: dict[str, Any],
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    log = logger or logging.getLogger("micro_quant_evolution")
    settings = micro_evolution_settings(config)
    if not micro_evolution_enabled(config):
        return {"status": "disabled", "positive_evolution": False}

    edge_db = EdgeDatabase(log)
    records = edge_db.load().get("records", [])
    min_trades = settings["min_trades_per_symbol"]
    threshold = settings["improvement_threshold_pct"]
    replay_bars = settings["replay_bars"]
    replay_step = settings["replay_step"]
    require_pos_exp = settings["require_positive_expectancy"]

    per_symbol_results: dict[str, dict[str, Any]] = {}
    evolved_count = 0
    total_improvement = 0.0

    for symbol in settings["symbols"]:
        opt = optimize_symbol_weights(records, symbol, min_trades=min_trades)
        if opt.get("status") != "candidate":
            per_symbol_results[symbol] = {**opt, "proposal": "hold", "reason": "insufficient_data"}
            continue

        try:
            baseline_out = _replay_symbol(
                config, symbol, None,
                replay_bars=replay_bars, replay_step=replay_step, logger=log,
            )
            candidate_out = _replay_symbol(
                config, symbol, opt["weights"],
                replay_bars=replay_bars, replay_step=replay_step, logger=log,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Micro evolution replay failed for %s: %s", symbol, exc)
            per_symbol_results[symbol] = {**opt, "proposal": "hold", "reason": f"replay_error:{exc}"}
            continue

        baseline_score = replay_fitness_score(baseline_out)
        candidate_score = replay_fitness_score(candidate_out)
        improvement = _improvement_pct(baseline_score, candidate_score)
        base_exp = replay_expectancy_usd(baseline_out)
        cand_exp = replay_expectancy_usd(candidate_out)

        positive_exp = cand_exp > base_exp if require_pos_exp else cand_exp >= base_exp
        deploy = improvement >= threshold and positive_exp and candidate_score > baseline_score

        sym_doc = {
            **opt,
            "baseline_score": round(baseline_score, 3),
            "candidate_score": round(candidate_score, 3),
            "improvement_pct": round(improvement, 2),
            "baseline_expectancy_usd": base_exp,
            "candidate_expectancy_usd": cand_exp,
            "baseline_pnl": baseline_out.get("pnl_total"),
            "candidate_pnl": candidate_out.get("pnl_total"),
            "proposal": "deploy" if deploy else "hold",
            "positive_evolution": deploy,
        }
        per_symbol_results[symbol] = sym_doc
        if deploy:
            evolved_count += 1
            total_improvement += improvement

    avg_improvement = total_improvement / evolved_count if evolved_count else 0.0
    global_weights = _merge_global_weights(per_symbol_results)
    positive_evolution = evolved_count > 0
    proposal = "deploy" if positive_evolution else "hold"

    candidate_doc: dict[str, Any] = {
        "timestamp": utc_now_iso(),
        "status": "candidate" if positive_evolution else "hold",
        "method": "micro_quant_evolution",
        "profile": config.get("active_profile", "30-c2"),
        "trade_count": len(records),
        "weights": global_weights,
        "per_symbol": per_symbol_results,
        "symbols_evolved": evolved_count,
        "replay_improvement_pct": round(avg_improvement, 2),
        "threshold_pct": threshold,
        "proposal": proposal,
        "deployed": False,
        "positive_evolution": positive_evolution,
        "message": (
            f"{evolved_count} symbol(s) beat baseline via replay (avg +{avg_improvement:.1f}%)"
            if positive_evolution
            else f"No symbol beat {threshold}% replay threshold"
        ),
    }

    if positive_evolution:
        write_json_state(STATE_CANDIDATES, candidate_doc)

    log_doc = read_json_state(STATE_EVOLUTION_LOG, default={"history": []}) or {}
    history = list(log_doc.get("history") or [])
    history.append({
        "timestamp": candidate_doc["timestamp"],
        "symbols_evolved": evolved_count,
        "proposal": proposal,
    })
    write_json_state(STATE_EVOLUTION_LOG, {
        "updated_at": utc_now_iso(),
        "last": candidate_doc,
        "history": history[-100:],
    })

    return {
        "status": "ok",
        "positive_evolution": positive_evolution,
        "proposal": proposal,
        "candidate": candidate_doc,
        "symbols_evolved": evolved_count,
    }