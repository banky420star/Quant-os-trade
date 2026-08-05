"""Practice-mode helpers — looser gates and session risk reset."""

from __future__ import annotations

import logging
from typing import Any

from core.account_mode import performance_gates_active
from core.daily_growth import growth_plan_enabled, reset_daily_growth_baseline
from core.micro_profile import micro_profile_enabled, micro_settings
from core.utils import read_json_state, utc_now_iso, write_json_state


def _live_equity(config: dict[str, Any]) -> float:
    account = read_json_state("account.json", default={})
    equity = account.get("equity") or account.get("balance")
    if equity is not None:
        return float(equity)
    return float(config.get("execution", {}).get("starting_cash", 100))


def sync_practice_gates(config: dict[str, Any]) -> dict[str, Any]:
    """Apply demo/practice trading gates when the $50k live plan is inactive."""
    if performance_gates_active(config):
        return config

    practice = config.get("practice") or {}
    growth = practice.get("growth") or {}
    growth_on = bool(growth.get("enabled", False))
    session = config.setdefault("session_scoring", {})
    signals = config.setdefault("signals", {})
    filters = config.setdefault("filters", {})
    risk = config.setdefault("risk", {})
    quant = config.setdefault("quant", {})
    trading = config.setdefault("trading", {})
    intel = config.setdefault("intelligence", {})
    app_cfg = config.setdefault("app", {})

    if growth_on:
        session["enabled"] = bool(growth.get("session_scoring_enabled", False))
        session["min_trade_score"] = int(growth.get("min_trade_score", 28))
        session["min_trade_score_aggressive"] = int(
            growth.get("min_trade_score_aggressive", session["min_trade_score"])
        )
        signals["min_confidence"] = int(growth.get("min_confidence", 52))
        signals["min_risk_reward"] = float(growth.get("min_risk_reward", 0.95))
        signals["default_risk_percent"] = float(growth.get("risk_percent_per_trade", 2.5))
        filters["min_volume_ratio"] = float(growth.get("min_volume_ratio", 0.12))
        filters["spread_mult"] = float(growth.get("spread_mult", 4.0))
        risk["max_drawdown_pct"] = float(growth.get("max_drawdown_pct", 30))
        risk["max_consecutive_losses"] = int(growth.get("max_consecutive_losses", 0))
        if micro_profile_enabled(config):
            micro = micro_settings(config)
            equity = float(micro.get("account_size_usd", 30))
            sym_frac = float(micro.get("max_symbol_exposure_fraction", 0.40))
            total_frac = float(micro.get("max_total_exposure_fraction", 0.60))
        else:
            equity = _live_equity(config)
            total_frac = float(growth.get("max_total_exposure_fraction", growth.get("max_exposure_fraction", 0.92)))
            sym_frac = float(growth.get("max_symbol_exposure_fraction", total_frac))
        risk["max_symbol_exposure_usd"] = round(equity * sym_frac, 2)
        risk["max_total_exposure_usd"] = round(equity * total_frac, 2)
        # Growth may deliberately disable the ranking engine, but it must not
        # silently rewrite the top-N policy for profiles that keep ranking on.
        if not bool(config.get("execution", {}).get("strategy_entries_market_only", False)):
            quant["strategy_ranking_enabled"] = bool(growth.get("strategy_ranking_enabled", False))
        quant["min_rank_win_rate"] = float(growth.get("min_rank_win_rate", 0))
        trading["aggressive_mode"] = bool(growth.get("aggressive_mode", True))
        trading["max_session_trades_per_symbol"] = int(
            growth.get("max_session_trades_per_symbol", 24)
        )
        if growth.get("dynamic_entries_enabled"):
            trading.setdefault("dynamic_entries", {})["enabled"] = True
        exec_cfg = config.setdefault("execution", {})
        if "max_lot" in growth and not bool(exec_cfg.get("fixed_exit_only", False)):
            exec_cfg["max_lot"] = float(growth["max_lot"])
        if growth.get("relax_consensus", True):
            intel["regime_veto_enabled"] = False
            intel["trend_structure_veto_enabled"] = False
            intel["risk_veto_threshold"] = int(growth.get("risk_veto_threshold", 6))
        if growth.get("loop_interval_seconds"):
            app_cfg["loop_interval_seconds"] = float(growth["loop_interval_seconds"])
    else:
        session["min_trade_score"] = int(practice.get("min_trade_score", 50))
        session["min_trade_score_aggressive"] = int(
            practice.get("min_trade_score_aggressive", session["min_trade_score"])
        )
        signals["min_confidence"] = int(practice.get("min_confidence", signals.get("min_confidence", 58)))
        if "min_risk_reward" in practice:
            signals["min_risk_reward"] = float(practice["min_risk_reward"])
        if "min_volume_ratio" in practice:
            filters["min_volume_ratio"] = float(practice["min_volume_ratio"])
        risk["max_drawdown_pct"] = float(practice.get("max_drawdown_pct", 25))
        if not micro_profile_enabled(config):
            equity_cap = float(config.get("execution", {}).get("starting_cash", 100))
            risk["max_symbol_exposure_usd"] = float(
                practice.get("max_symbol_exposure_usd", equity_cap)
            )
            risk["max_total_exposure_usd"] = float(
                practice.get("max_total_exposure_usd", equity_cap)
            )

    if "require_top_ranked_setup" in practice and not growth_on:
        quant["require_top_ranked_setup"] = bool(practice["require_top_ranked_setup"])
    if "ranking_flex_min_win_rate" in practice:
        quant["ranking_flex_min_win_rate"] = float(practice["ranking_flex_min_win_rate"])
    if practice.get("symbols"):
        config.setdefault("mt5", {})["symbols"] = list(practice["symbols"])
    if practice.get("relax_structure_checks"):
        practice.setdefault("relax_structure_checks", True)
    if practice.get("relax_volume_check"):
        practice.setdefault("relax_volume_check", True)
    return config


def ensure_practice_session(
    config: dict[str, Any],
    logger: logging.Logger | None = None,
    *,
    force_rebaseline: bool = False,
) -> dict[str, Any]:
    """Reset kill switch + MT5 drawdown baseline for a fresh practice run."""
    log = logger or logging.getLogger("practice_session")
    if performance_gates_active(config):
        return {"skipped": True, "reason": "live_plan_active"}

    account = read_json_state("account.json", default={})
    kill = read_json_state("kill_switch.json", default={"kill_switch": False})
    prior_baseline = read_json_state("mt5_baseline.json", default={}) or {}
    equity = account.get("equity") or account.get("balance")
    login_changed = (
        account.get("login") is not None
        and prior_baseline.get("login") is not None
        and int(account["login"]) != int(prior_baseline["login"])
    )
    needs_baseline = not prior_baseline.get("starting_cash")

    report: dict[str, Any] = {
        "timestamp": utc_now_iso(),
        "rebaseline": False,
        "kill_switch_cleared": False,
    }

    if equity is not None and (force_rebaseline or login_changed or needs_baseline):
        equity_f = float(equity)
        baseline = {
            "login": account.get("login"),
            "server": account.get("server"),
            "starting_cash": equity_f,
            "set_at": utc_now_iso(),
            "source": "practice_session_reset",
        }
        write_json_state("mt5_baseline.json", baseline)
        report["rebaseline"] = True
        report["starting_cash"] = equity_f
        log.info("Practice baseline reset to current MT5 equity: %.2f", equity_f)
        reset_daily_growth_baseline(equity_f, config)
        report["daily_growth_reset"] = True
        log.info("Daily growth baseline reset to %.2f (login=%s)", equity_f, account.get("login"))
    elif equity is not None:
        log.info(
            "Practice baseline unchanged (login=%s equity=%.2f)",
            account.get("login"),
            float(equity),
        )

    if kill.get("kill_switch") or force_rebaseline:
        write_json_state("kill_switch.json", {
            "kill_switch": False,
            "reason": None,
            "activated_at": None,
            "cleared_at": utc_now_iso(),
            "cleared_by": "practice_session_reset",
        })
        report["kill_switch_cleared"] = True
        log.info("Practice kill switch cleared")

    return report