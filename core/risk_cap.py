"""Per-trade USD risk cap — shared by MT5 broker and micro profile."""

from __future__ import annotations

from typing import Any

from core.blue_guardian import blue_guardian_enabled, blue_guardian_settings


def risk_per_trade_cap(config: dict[str, Any]) -> float | None:
    """Max dollars at risk on a single trade (stop-loss distance × size)."""
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


def cap_loss_to_balance_enabled(config: dict[str, Any]) -> bool:
    risk = config.get("risk") or {}
    if "cap_loss_to_balance" in risk:
        return bool(risk["cap_loss_to_balance"])
    micro = (config.get("practice") or {}).get("micro") or {}
    if micro.get("enabled"):
        return bool(micro.get("cap_loss_to_balance", True))
    trading = config.get("trading") or {}
    return bool(trading.get("cap_loss_to_balance", False))


def effective_risk_cap(config: dict[str, Any], balance: float) -> float | None:
    """USD risk budget for one trade — never above balance when capped."""
    base = risk_per_trade_cap(config)
    bal = max(0.0, float(balance))
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