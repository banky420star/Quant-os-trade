"""Exposure calculation and equity-based position sizing."""

from __future__ import annotations

import math
from typing import Any

from core.kelly_sizing import resolve_risk_percent
from core.micro_profile import independent_symbol_exposure
from core.risk_cap import estimate_stop_loss_usd
from core.trade_limits import unlimited_trades


def _risk_exposure_spec(spec: dict[str, float] | None) -> bool:
    if not spec:
        return False
    tick_value = float(spec.get("trade_tick_value") or 0)
    tick_size = float(spec.get("trade_tick_size") or spec.get("point") or 0)
    return tick_value > 0 and tick_size > 0


def position_exposure_usd(
    entry: float,
    size: float,
    *,
    sl: float | None = None,
    symbol_spec: dict[str, float] | None = None,
) -> float:
    """USD exposure — stop-loss risk when tick specs exist, else price×lot notional."""
    size_f = abs(float(size))
    if size_f <= 0:
        return 0.0
    if sl is not None and _risk_exposure_spec(symbol_spec):
        risk_dist = abs(float(entry) - float(sl))
        return estimate_stop_loss_usd(
            risk_dist=risk_dist,
            volume=size_f,
            tick_value=float(symbol_spec["trade_tick_value"]),
            tick_size=float(symbol_spec.get("trade_tick_size") or symbol_spec.get("point") or 0),
        )
    return position_notional(entry, size_f)


def exposure_metric_for_config(
    config: dict[str, Any],
    *,
    symbol_specs: dict[str, dict[str, float]] | None = None,
) -> str:
    """'risk_usd' for micro/MT5 executable sizing; 'notional' otherwise."""
    if symbol_specs:
        from core.position_sizing import requires_executable_sizing

        if requires_executable_sizing(config):
            return "risk_usd"
    else:
        from core.position_sizing import requires_executable_sizing

        if requires_executable_sizing(config):
            return "risk_usd"
    return "notional"


def calc_risk_based_size(
    equity: float,
    risk_percent: float,
    entry: float,
    sl: float,
    *,
    max_size: float | None = None,
    min_size: float = 0.0001,
) -> float:
    """Position size from equity risk budget: risk_money / |entry - sl|."""
    if equity <= 0 or entry <= 0:
        return min_size

    risk_money = equity * (risk_percent / 100.0)
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return min_size

    size = risk_money / risk_per_unit
    if max_size is not None:
        size = min(size, max_size)
    return max(min_size, round(size, 4))


def position_notional(entry: float, size: float) -> float:
    """USD notional exposure for a position."""
    return abs(float(entry) * float(size))


def exposure_from_positions(
    positions: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
    symbol_specs: dict[str, dict[str, float]] | None = None,
) -> tuple[dict[str, float], float]:
    """Return per-symbol and total exposure (risk USD or notional)."""
    use_risk = config is not None and exposure_metric_for_config(config, symbol_specs=symbol_specs) == "risk_usd"
    symbol_exposure: dict[str, float] = {}
    for pos in positions:
        symbol = pos["symbol"]
        spec = (symbol_specs or {}).get(symbol) if use_risk else None
        if use_risk and spec and pos.get("sl") is not None:
            exp = position_exposure_usd(
                pos.get("entry", 0),
                pos.get("size", 0.01),
                sl=float(pos["sl"]),
                symbol_spec=spec,
            )
        else:
            exp = position_notional(pos.get("entry", 0), pos.get("size", 0.01))
        symbol_exposure[symbol] = symbol_exposure.get(symbol, 0.0) + exp
    return symbol_exposure, sum(symbol_exposure.values())


def exposure_used_pct(total_exposure: float, max_total_exposure: float) -> float:
    """Percentage of total exposure limit currently in use."""
    if max_total_exposure <= 0:
        return 0.0
    return round(min(100.0, total_exposure / max_total_exposure * 100.0), 2)


def max_size_for_exposure(
    entry: float,
    remaining_exposure: float,
    *,
    min_size: float = 0.0001,
    sl: float | None = None,
    symbol_spec: dict[str, float] | None = None,
) -> float:
    """Largest size that keeps exposure <= remaining_exposure."""
    if remaining_exposure <= 0:
        return 0.0
    if sl is not None and _risk_exposure_spec(symbol_spec):
        risk_dist = abs(float(entry) - float(sl))
        risk_per_lot = estimate_stop_loss_usd(
            risk_dist=risk_dist,
            volume=1.0,
            tick_value=float(symbol_spec["trade_tick_value"]),
            tick_size=float(symbol_spec.get("trade_tick_size") or symbol_spec.get("point") or 0),
        )
        if risk_per_lot <= 0:
            return 0.0
        return max(0.0, remaining_exposure / risk_per_lot)
    if entry <= 0:
        return 0.0
    return max(0.0, remaining_exposure / entry)


def check_exposure_limits(
    positions: list[dict[str, Any]],
    signal: dict[str, Any],
    equity: float,
    config: dict[str, Any],
    *,
    balance: float | None = None,
    symbol_specs: dict[str, dict[str, float]] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """
    Check whether a new signal would exceed exposure limits.

    Returns (ok, details) where details includes projected size/exposure.
    """
    if unlimited_trades(config):
        entry = float(signal.get("entry", 0))
        sl = float(signal.get("sl", 0))
        equity_f = float(equity)
        default_risk = float(config.get("signals", {}).get("default_risk_percent", 1))
        risk_pct, kelly = resolve_risk_percent(signal, config, default_risk)
        exec_cfg = config.get("execution", {})
        max_lot = float(exec_cfg.get("max_lot", 0.1)) if config.get("execution", {}).get("mode") == "mt5" else None
        ideal_size = calc_risk_based_size(equity_f, risk_pct, entry, sl, max_size=max_lot)
        return True, {
            "equity": round(equity_f, 2),
            "ideal_size": ideal_size,
            "projected_notional": round(position_notional(entry, ideal_size), 2),
            "unlimited_trades": True,
            "kelly": kelly,
            "risk_percent": risk_pct,
        }

    risk_cfg = config.get("risk", {})
    signals_cfg = config.get("signals", {})
    exec_cfg = config.get("execution", {})

    default_risk = float(signals_cfg.get("default_risk_percent", 1))
    risk_pct, kelly = resolve_risk_percent(signal, config, default_risk)
    max_symbol = float(risk_cfg.get("max_symbol_exposure_usd", 100))
    max_total = float(risk_cfg.get("max_total_exposure_usd", 300))
    max_lot = float(exec_cfg.get("max_lot", 0.1)) if config.get("execution", {}).get("mode") == "mt5" else None

    entry = float(signal.get("entry", 0))
    sl = float(signal.get("sl", 0))
    symbol = signal["symbol"]

    bal = float(balance if balance is not None else equity)
    from core.position_sizing import calc_executable_volume, requires_executable_sizing, resolve_symbol_spec

    if requires_executable_sizing(config):
        spec = resolve_symbol_spec(symbol, config, overrides=symbol_specs)
        exec_vol, exec_details = calc_executable_volume(
            signal,
            equity=equity,
            balance=bal,
            config=config,
            symbol_spec=spec,
            open_positions=positions,
            # 2026-08-03 — forward the FULL symbol-spec map so open positions
            # on OTHER symbols are measured in SL-risk USD, not notional.
            # Without this, a 0.25-lot US30m open counted ~$13,301 notional
            # and every index-CFD candidate was rejected (exposure_limit
            # _exceeded / exposure_cap_below_min_lot).
            symbol_specs=symbol_specs,
        )
        ideal_size = float(exec_details.get("ideal_size", 0))
        capped_size = exec_vol
        allowed = exec_vol > 0
        projected_notional = (
            position_exposure_usd(entry, capped_size, sl=sl, symbol_spec=spec)
            if capped_size > 0
            else 0.0
        )
    else:
        ideal_size = calc_risk_based_size(equity, risk_pct, entry, sl, max_size=max_lot)
        capped_size, allowed = cap_size_to_exposure_limits(
            ideal_size,
            entry,
            symbol,
            positions,
            config,
        )
        projected_notional = position_notional(entry, capped_size)
        exec_details = {}

    symbol_exp, total_exp = exposure_from_positions(positions, config=config, symbol_specs=symbol_specs)
    current_symbol = symbol_exp.get(symbol, 0.0)

    symbol_after = current_symbol + projected_notional
    total_after = total_exp + projected_notional

    symbol_ok = allowed and symbol_after <= max_symbol + 0.01
    if independent_symbol_exposure(config):
        total_ok = allowed
    else:
        total_ok = allowed and total_after <= max_total + 0.01

    details = {
        "equity": round(equity, 2),
        "ideal_size": ideal_size,
        "capped_size": capped_size,
        "projected_notional": round(projected_notional, 2),
        "current_symbol_exposure": round(current_symbol, 2),
        "current_total_exposure": round(total_exp, 2),
        "symbol_after": round(symbol_after, 2),
        "total_after": round(total_after, 2),
        "max_symbol_exposure": max_symbol,
        "max_total_exposure": max_total,
        "symbol_ok": symbol_ok,
        "total_ok": total_ok,
        "exposure_allowed": allowed,
        "kelly": kelly,
        "risk_percent": risk_pct,
        "executable_volume": capped_size if requires_executable_sizing(config) else None,
    }
    if exec_details:
        details.update({k: v for k, v in exec_details.items() if k not in details})

    ok = allowed and symbol_ok and total_ok
    if requires_executable_sizing(config) and capped_size <= 0:
        ok = False
    return ok, details


def cap_size_to_exposure_limits(
    size: float,
    entry: float,
    symbol: str,
    positions: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    min_size: float = 0.0001,
    sl: float | None = None,
    symbol_spec: dict[str, float] | None = None,
    symbol_specs: dict[str, dict[str, float]] | None = None,
) -> tuple[float, bool]:
    """
    Cap position size so symbol and total exposure limits are respected.

    Returns (capped_size, allowed). allowed=False if even min_size exceeds limits.
    """
    if unlimited_trades(config):
        return round(max(size, min_size), 4), True

    risk_cfg = config.get("risk", {})
    max_symbol = float(risk_cfg.get("max_symbol_exposure_usd", 100))
    max_total = float(risk_cfg.get("max_total_exposure_usd", 300))

    specs = symbol_specs or ({symbol: symbol_spec} if symbol_spec else None)
    symbol_exp, total_exp = exposure_from_positions(positions, config=config, symbol_specs=specs)
    symbol_remaining = max(0.0, max_symbol - symbol_exp.get(symbol, 0.0))
    if independent_symbol_exposure(config):
        remaining = symbol_remaining
    else:
        total_remaining = max(0.0, max_total - total_exp)
        remaining = min(symbol_remaining, total_remaining)

    max_allowed = max_size_for_exposure(
        entry,
        remaining,
        min_size=min_size,
        sl=sl,
        symbol_spec=symbol_spec,
    )
    capped = min(size, max_allowed)

    if capped < min_size:
        return 0.0, False
    # Floor (never round up) to lot precision: rounding a capped size UP would
    # push its notional back over the very exposure cap we just enforced, and
    # the downstream check_exposure_limits (tolerance +$0.01) would then reject
    # the trade for being a few cents over. Truncating keeps notional <= cap.
    floored = math.floor(capped * 10000) / 10000
    if floored < min_size:
        # Capped size rounds below the min lot — fall back to min_size only if
        # min_size itself fits (max_allowed >= min_size), else disallow.
        return (min_size, True) if max_allowed >= min_size else (0.0, False)
    return floored, True