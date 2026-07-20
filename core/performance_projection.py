"""Monthly PnL projection from replay results — capital-scaled extrapolation."""

from __future__ import annotations

from typing import Any

TARGET_MONTHLY_PNL_USD = 50_000.0
PROJECTION_CAPITAL_USD = 1_000_000.0
M5_MINUTES_PER_BAR = 5
DAYS_PER_MONTH = 30.0
DEFAULT_MIN_REPLAY_WINDOW_DAYS = 7.0
DEFAULT_MIN_CLOSED_TRADES = 5


def replay_window_days(bars_replayed: int, bars_per_step: int) -> float:
    """Calendar days covered by a replay walk (M5 bars × step)."""
    if bars_replayed <= 0 or bars_per_step <= 0:
        return 0.0
    minutes = bars_replayed * bars_per_step * M5_MINUTES_PER_BAR
    return minutes / (60.0 * 24.0)


def scale_pnl_to_capital(
    pnl: float,
    replay_starting_cash: float,
    target_capital: float = PROJECTION_CAPITAL_USD,
) -> float:
    """Linear scale of replay PnL to a target capital base."""
    if replay_starting_cash <= 0:
        return 0.0
    return round(float(pnl) * (float(target_capital) / float(replay_starting_cash)), 2)


def project_monthly_pnl(
    pnl_total: float,
    bars_replayed: int,
    bars_per_step: int,
    *,
    replay_starting_cash: float,
    target_capital: float = PROJECTION_CAPITAL_USD,
    target_monthly_pnl: float = TARGET_MONTHLY_PNL_USD,
) -> dict[str, Any]:
    """
    Extrapolate replay PnL to a 30-day month on the target capital base.

    Formula:
      scaled_pnl = pnl_total × (target_capital / replay_starting_cash)
      daily_pnl = scaled_pnl / replay_window_days
      monthly_pnl = daily_pnl × 30
    """
    window_days = replay_window_days(bars_replayed, bars_per_step)
    scaled_pnl = scale_pnl_to_capital(pnl_total, replay_starting_cash, target_capital)
    if window_days <= 0:
        daily_pnl = 0.0
        monthly_pnl = 0.0
    else:
        daily_pnl = round(scaled_pnl / window_days, 2)
        monthly_pnl = round(daily_pnl * DAYS_PER_MONTH, 2)

    gap = round(target_monthly_pnl - monthly_pnl, 2)
    return {
        "replay_pnl": round(float(pnl_total), 2),
        "scaled_pnl": scaled_pnl,
        "replay_starting_cash": round(float(replay_starting_cash), 2),
        "target_capital": round(float(target_capital), 2),
        "bars_replayed": int(bars_replayed),
        "bars_per_step": int(bars_per_step),
        "replay_window_days": round(window_days, 4),
        "daily_pnl": daily_pnl,
        "projected_monthly_pnl": monthly_pnl,
        "target_monthly_pnl": round(float(target_monthly_pnl), 2),
        "gap_to_target": gap,
        "meets_pnl_target": monthly_pnl >= target_monthly_pnl,
        # Back-compat alias — benchmark runner applies coverage via benchmark_meets_target().
        "meets_target": monthly_pnl >= target_monthly_pnl,
    }


def benchmark_meets_target(
    projection: dict[str, Any],
    *,
    trades_closed: int,
    min_replay_window_days: float = DEFAULT_MIN_REPLAY_WINDOW_DAYS,
    min_closed_trades: int = DEFAULT_MIN_CLOSED_TRADES,
    target_monthly_pnl: float = TARGET_MONTHLY_PNL_USD,
) -> dict[str, Any]:
    """Eligibility gate: PnL target AND sufficient replay coverage."""
    window_days = float(projection.get("replay_window_days", 0))
    monthly_pnl = float(projection.get("projected_monthly_pnl", 0))
    window_ok = window_days >= float(min_replay_window_days)
    trades_ok = int(trades_closed) >= int(min_closed_trades)
    pnl_ok = monthly_pnl >= float(target_monthly_pnl)
    coverage_ok = window_ok and trades_ok
    return {
        "replay_window_days": round(window_days, 4),
        "min_replay_window_days": float(min_replay_window_days),
        "window_ok": window_ok,
        "trades_closed": int(trades_closed),
        "min_closed_trades": int(min_closed_trades),
        "trades_ok": trades_ok,
        "projected_monthly_pnl": round(monthly_pnl, 2),
        "target_monthly_pnl": round(float(target_monthly_pnl), 2),
        "pnl_ok": pnl_ok,
        "coverage_ok": coverage_ok,
        "meets_target": coverage_ok and pnl_ok,
        "gap_to_target": round(target_monthly_pnl - monthly_pnl, 2),
    }


def aggregate_symbol_projections(
    symbol_results: list[dict[str, Any]],
    bars_per_step: int,
    *,
    replay_starting_cash: float,
    target_capital: float = PROJECTION_CAPITAL_USD,
    target_monthly_pnl: float = TARGET_MONTHLY_PNL_USD,
) -> dict[str, Any]:
    """Sum per-symbol replay PnL, then project once on the combined series.

    Each symbol replays the same calendar window in parallel (live agent runs
    all symbols on one clock). Use the longest single-symbol bar span for the
    combined window — not the sum of per-symbol bar counts.
    """
    total_pnl = sum(float(r.get("pnl_total", 0)) for r in symbol_results)
    bar_counts = [int(r.get("bars_replayed", 0)) for r in symbol_results]
    window_bars = max(bar_counts) if bar_counts else 0
    projection = project_monthly_pnl(
        total_pnl,
        window_bars,
        bars_per_step,
        replay_starting_cash=replay_starting_cash,
        target_capital=target_capital,
        target_monthly_pnl=target_monthly_pnl,
    )
    per_symbol = []
    for result in symbol_results:
        sym_proj = project_monthly_pnl(
            float(result.get("pnl_total", 0)),
            int(result.get("bars_replayed", 0)),
            bars_per_step,
            replay_starting_cash=replay_starting_cash,
            target_capital=target_capital,
            target_monthly_pnl=target_monthly_pnl,
        )
        per_symbol.append({
            "symbol": result.get("symbol"),
            "pnl_total": result.get("pnl_total"),
            "trades_closed": result.get("trades_closed"),
            "win_rate_pct": result.get("win_rate_pct"),
            "bars_replayed": result.get("bars_replayed"),
            "projected_monthly_pnl": sym_proj["projected_monthly_pnl"],
        })

    return {
        "symbols": per_symbol,
        "combined_pnl_total": round(total_pnl, 2),
        "combined_bars_replayed": window_bars,
        "per_symbol_bars_replayed": bar_counts,
        **projection,
    }