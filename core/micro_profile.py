"""$30 micro-account profile — tight symbols, no pyramiding, capped exposure."""

from __future__ import annotations

from typing import Any

from core.utils import read_json_state


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def micro_profile_enabled(config: dict[str, Any]) -> bool:
    micro = (config.get("practice") or {}).get("micro") or {}
    return bool(micro.get("enabled", False))


def micro_settings(config: dict[str, Any]) -> dict[str, Any]:
    return dict((config.get("practice") or {}).get("micro") or {})


def micro_reference_equity(config: dict[str, Any]) -> float:
    """Sizing reference: live MT5 balance on micro-live, else configured account_size_usd."""
    micro = micro_settings(config)
    if bool(micro.get("live_mode", False)):
        account = read_json_state("account.json", default={})
        for key in ("equity", "balance"):
            val = account.get(key)
            if val is not None and float(val) > 0:
                return float(val)
    return float(micro.get("account_size_usd", 30))


def sync_micro_profile(config: dict[str, Any]) -> dict[str, Any]:
    """Apply micro-account overrides after practice/arena symbol sync."""
    if not micro_profile_enabled(config):
        return config

    micro = micro_settings(config)
    symbols = list(micro.get("symbols") or [])
    if symbols:
        config.setdefault("mt5", {})["symbols"] = symbols
        config.setdefault("practice", {})["symbols"] = symbols
        config.setdefault("strategy_arena", {})["symbols"] = symbols

    mop = max(1, int(micro.get("max_open_per_symbol", 1)))
    trading = config.setdefault("trading", {})
    trading["max_open_per_symbol"] = mop
    trading["allow_pyramiding"] = bool(micro.get("allow_pyramiding", False))
    if micro.get("aggressive_mode") is not None:
        trading["aggressive_mode"] = bool(micro["aggressive_mode"])
    if micro.get("max_session_trades_per_symbol") is not None:
        trading["max_session_trades_per_symbol"] = int(micro["max_session_trades_per_symbol"])
    if micro.get("reentry_cooldown_seconds") is not None:
        trading["reentry_cooldown_seconds"] = float(micro["reentry_cooldown_seconds"])
    if micro.get("entry_confirm_seconds") is not None:
        trading["entry_confirm_seconds"] = float(micro["entry_confirm_seconds"])
    if micro.get("regime_flip_replace_enabled") is not None:
        trading["regime_flip_replace_enabled"] = bool(micro["regime_flip_replace_enabled"])
    if micro.get("regime_flip_min_confidence") is not None:
        trading["regime_flip_min_confidence"] = int(micro["regime_flip_min_confidence"])
    if micro.get("regime_flip_window_sec") is not None:
        trading["regime_flip_window_sec"] = int(micro["regime_flip_window_sec"])

    risk = config.setdefault("risk", {})
    if micro.get("max_loss_per_trade_usd") is not None:
        risk["max_loss_per_trade_usd"] = float(micro["max_loss_per_trade_usd"])
        trading["max_loss_per_trade_usd"] = float(micro["max_loss_per_trade_usd"])
    if micro.get("cap_loss_to_balance") is not None:
        risk["cap_loss_to_balance"] = bool(micro["cap_loss_to_balance"])
        trading["cap_loss_to_balance"] = bool(micro["cap_loss_to_balance"])

    micro_be = micro.get("break_even")
    if isinstance(micro_be, dict) and micro_be:
        trading["break_even"] = _deep_merge(dict(trading.get("break_even") or {}), micro_be)

    micro_trailing = micro.get("trailing")
    if isinstance(micro_trailing, dict) and micro_trailing:
        trading["trailing"] = _deep_merge(dict(trading.get("trailing") or {}), micro_trailing)
        trading["trailing"]["enabled"] = True

    micro_exits = micro.get("exits")
    if isinstance(micro_exits, dict) and micro_exits:
        trading["exits"] = _deep_merge(dict(trading.get("exits") or {}), micro_exits)

    arena = config.setdefault("strategy_arena", {})
    arena["max_open_per_symbol"] = mop
    if micro.get("max_open_per_setup") is not None:
        arena["max_open_per_setup"] = max(1, int(micro["max_open_per_setup"]))

    exec_cfg = config.setdefault("execution", {})
    if micro.get("max_lot") is not None:
        exec_cfg["max_lot"] = float(micro["max_lot"])
    if micro.get("default_lot") is not None:
        exec_cfg["default_lot"] = float(micro["default_lot"])

    ref_equity = micro_reference_equity(config)
    sym_frac = float(micro.get("max_symbol_exposure_fraction", 0.40))
    total_frac = float(micro.get("max_total_exposure_fraction", 0.60))
    risk["max_symbol_exposure_usd"] = round(ref_equity * sym_frac, 2)
    risk["max_total_exposure_usd"] = round(ref_equity * total_frac, 2)
    if micro.get("max_drawdown_pct") is not None:
        risk["max_drawdown_pct"] = float(micro["max_drawdown_pct"])

    signals = config.setdefault("signals", {})
    if micro.get("risk_percent_per_trade") is not None:
        signals["default_risk_percent"] = float(micro["risk_percent_per_trade"])

    live_mode = bool(micro.get("live_mode", False))
    growth = config.setdefault("practice", {}).setdefault("growth", {})
    if live_mode or micro.get("growth_enabled") is False:
        growth["enabled"] = False
    else:
        growth["enabled"] = True
    if micro.get("campaign_days") is not None:
        growth["campaign_days"] = int(micro["campaign_days"])
    elif not growth.get("campaign_days"):
        growth["campaign_days"] = 30
    for key in ("daily_target_pct", "max_daily_loss_pct", "risk_percent_per_trade",
                "max_session_trades_per_symbol", "max_lot", "max_drawdown_pct"):
        if micro.get(key) is not None:
            growth[key] = micro[key]

    filters = config.setdefault("filters", {})
    if micro.get("avoid_news") is not None:
        filters["avoid_news"] = bool(micro["avoid_news"])
    elif "avoid_news" not in filters:
        filters["avoid_news"] = True

    sym_rules = micro.get("symbol_rules")
    if isinstance(sym_rules, dict) and sym_rules:
        base_rules = dict(signals.get("symbol_rules") or {})
        base_rules.update(sym_rules)
        signals["symbol_rules"] = base_rules

    if live_mode:
        exec_cfg["starting_cash"] = ref_equity
        if micro.get("aggressive_mode"):
            trading["aggressive_mode"] = True
        arena = config.setdefault("strategy_arena", {})
        if micro.get("disable_arena", True):
            arena["enabled"] = False
            arena["symbols"] = symbols or arena.get("symbols") or []

    config["micro_profile_active"] = True
    config["micro_live_mode"] = live_mode
    return config