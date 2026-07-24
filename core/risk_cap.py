"""Per-trade USD risk cap — shared by MT5 broker and micro profile."""

from __future__ import annotations

from typing import Any

from core.blue_guardian import blue_guardian_enabled, blue_guardian_settings


def _symbol_loss_cap(config: dict[str, Any], symbol: str | None) -> float | None:
    if not symbol:
        return None
    for rules_key in ("signals", "practice"):
        sym_rules = (config.get(rules_key) or {}).get("symbol_rules") or {}
        rule = sym_rules.get(symbol) or {}
        if rule.get("max_loss_per_trade_usd") is not None:
            return float(rule["max_loss_per_trade_usd"])
    micro = (config.get("practice") or {}).get("micro") or {}
    micro_rule = (micro.get("symbol_rules") or {}).get(symbol) or {}
    if micro_rule.get("max_loss_per_trade_usd") is not None:
        return float(micro_rule["max_loss_per_trade_usd"])
    return None


def _global_static_loss_cap(config: dict[str, Any]) -> float | None:
    """Fixed-dollar per-trade cap from global config (no per-symbol rule)."""
    if blue_guardian_enabled(config):
        return float(blue_guardian_settings(config)["risk_per_trade_usd"])

    trading = config.get("trading") or {}
    if trading.get("max_loss_per_trade_usd") is not None:
        return float(trading["max_loss_per_trade_usd"])

    risk = config.get("risk") or {}
    if risk.get("max_loss_per_trade_usd") is not None:
        return float(risk["max_loss_per_trade_usd"])

    micro = (config.get("practice") or {}).get("micro") or {}
    if micro.get("enabled") and micro.get("max_loss_per_trade_usd") is not None:
        return float(micro["max_loss_per_trade_usd"])

    return None


def risk_per_trade_pct(config: dict[str, Any], symbol: str | None = None) -> float | None:
    """Configured per-trade risk as a PERCENT of account equity (account-aware).

    Checked in priority: per-symbol rule -> risk.max_risk_per_trade_pct. Returns
    None when no percentage basis is configured (falls back to the static cap).
    """
    if symbol:
        for rules_key in ("signals", "practice"):
            rule = ((config.get(rules_key) or {}).get("symbol_rules") or {}).get(symbol) or {}
            if rule.get("max_risk_per_trade_pct") is not None:
                return float(rule["max_risk_per_trade_pct"])
    risk = config.get("risk") or {}
    if risk.get("max_risk_per_trade_pct") is not None:
        return float(risk["max_risk_per_trade_pct"])
    return None


def has_per_trade_cap(config: dict[str, Any], symbol: str | None = None) -> bool:
    """True when any per-trade cap (static dollars OR percent of equity) applies."""
    return (
        _symbol_loss_cap(config, symbol) is not None
        or _global_static_loss_cap(config) is not None
        or risk_per_trade_pct(config, symbol) is not None
    )


def risk_per_trade_cap(
    config: dict[str, Any],
    symbol: str | None = None,
    equity: float | None = None,
) -> float | None:
    """Max dollars at risk on a single trade — account-size-aware.

    Precedence:
      1. An **explicit per-symbol** dollar cap is authoritative — the operator
         deliberately set it for this symbol, so it is respected as-is.
      2. Otherwise the cap is the tighter (min) of a **percent of live equity**
         (``risk.max_risk_per_trade_pct`` — scales with the account, the
         professional default) and any **global static dollar** ceiling.
    When ``equity`` is None the percent cap can't be priced, so only the static
    caps apply (backward compatible with callers that don't pass equity).
    """
    sym_cap = _symbol_loss_cap(config, symbol)
    if sym_cap is not None:
        return sym_cap

    static_cap = _global_static_loss_cap(config)
    pct = risk_per_trade_pct(config, symbol)
    pct_cap: float | None = None
    if pct is not None and equity is not None and equity > 0:
        pct_cap = float(equity) * float(pct) / 100.0

    caps = [c for c in (static_cap, pct_cap) if c is not None]
    if not caps:
        return None
    return min(caps)


def cap_loss_to_balance_enabled(config: dict[str, Any]) -> bool:
    risk = config.get("risk") or {}
    if "cap_loss_to_balance" in risk:
        return bool(risk["cap_loss_to_balance"])
    micro = (config.get("practice") or {}).get("micro") or {}
    if micro.get("enabled"):
        return bool(micro.get("cap_loss_to_balance", True))
    trading = config.get("trading") or {}
    return bool(trading.get("cap_loss_to_balance", False))


def effective_risk_cap(config: dict[str, Any], balance: float, *, symbol: str | None = None) -> float | None:
    """USD risk budget for one trade — never above balance when capped.

    ``balance`` is the live account equity, so the percent-of-equity cap in
    ``risk_per_trade_cap`` is priced against the real account size here.
    """
    bal = max(0.0, float(balance))
    base = risk_per_trade_cap(config, symbol=symbol, equity=bal if bal > 0 else None)
    if not cap_loss_to_balance_enabled(config):
        return base
    if base is None:
        return bal if bal > 0 else None
    return min(base, bal)


def estimate_stop_loss_usd(
    *,
    risk_dist: float,
    volume: float,
    tick_value: float = 0.0,
    tick_size: float = 0.0,
) -> float:
    """USD loss if price travels from entry to stop at ``volume`` lots."""
    if risk_dist <= 0 or volume <= 0:
        return 0.0
    if tick_value > 0 and tick_size > 0:
        ticks = risk_dist / tick_size
        return float(ticks * tick_value * volume)
    return float(risk_dist * volume)