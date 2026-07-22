"""Blue Guardian Instant Standard risk rules for funded accounts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from core.utils import read_json_state, utc_now_iso, write_json_state

STATE_FILE = "blue_guardian.json"
HIGH_WATERMARK_FILE = "blue_guardian_high_watermark.json"
OPEN_TIMES_FILE = "position_open_times.json"

_EST = ZoneInfo("America/New_York")


def blue_guardian_enabled(config: dict[str, Any]) -> bool:
    return bool((config.get("blue_guardian") or {}).get("enabled", False))


def _coerce_int(value: Any, default: int) -> int:
    try:
        if value is None or str(value).strip() == "":
            return int(default)
        if str(value).strip().lower() == "auto":
            return int(default)
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _coerce_float(value: Any, default: float) -> float:
    try:
        if value is None or str(value).strip() == "":
            return float(default)
        if str(value).strip().lower() == "auto":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _resolve_max_total_open_positions(config: dict[str, Any], bg: dict[str, Any]) -> int:
    """One slot per TUI symbol when one_trade_per_symbol or max_total=auto."""
    per_sym = _coerce_int(bg.get("max_open_per_symbol", 1), 1)
    symbols = list((config.get("mt5") or {}).get("symbols") or [])
    raw_total = bg.get("max_total_open_positions", "auto")
    if bg.get("one_trade_per_symbol", False) or str(raw_total).lower() == "auto":
        return max(1, len(symbols) * per_sym)
    return _coerce_int(raw_total, max(1, len(symbols) * per_sym))


def blue_guardian_settings(config: dict[str, Any]) -> dict[str, Any]:
    bg = dict(config.get("blue_guardian") or {})
    size = _coerce_float(bg.get("account_size_usd", 5000), 5000.0)
    target_pct = _coerce_float(bg.get("daily_profit_target_pct", 6), 6.0)
    max_total = _resolve_max_total_open_positions(config, bg)
    return {
        "enabled": bool(bg.get("enabled", False)),
        "account_size_usd": size,
        "daily_profit_target_pct": target_pct,
        "daily_profit_target_usd": float(bg.get("daily_profit_target_usd", size * target_pct / 100)),
        "daily_profit_lock_on_hit": bool(bg.get("daily_profit_lock_on_hit", False)),
        "max_open_per_symbol": _coerce_int(bg.get("max_open_per_symbol", 1), 1),
        "max_total_open_positions": max_total,
        "allow_pyramiding": bool(bg.get("allow_pyramiding", False)),
        "per_trade_normal_loss_usd": _coerce_float(bg.get("per_trade_normal_loss_usd", 25), 25.0),
        "per_trade_hard_loss_usd": _coerce_float(bg.get("per_trade_hard_loss_usd", 30), 30.0),
        "per_trade_emergency_usd": _coerce_float(bg.get("per_trade_emergency_usd", 35), 35.0),
        "floating_block_new_trades_usd": _coerce_float(bg.get("floating_block_new_trades_usd", -35), -35.0),
        "floating_close_all_usd": _coerce_float(bg.get("floating_close_all_usd", -45), -45.0),
        "guardian_shield_usd": _coerce_float(bg.get("guardian_shield_usd", -50), -50.0),
        "max_daily_loss_usd": _coerce_float(bg.get("max_daily_loss_usd", size * 0.03), size * 0.03),
        "daily_loss_buffer_usd": _coerce_float(bg.get("daily_loss_buffer_usd", 140), 140.0),
        "max_trailing_drawdown_pct": _coerce_float(bg.get("max_trailing_drawdown_pct", 6), 6.0),
        "min_hold_seconds": _coerce_int(bg.get("min_hold_seconds", 130), 130),
        "daily_reset_hour_est": _coerce_int(bg.get("daily_reset_hour_est", 17), 17),
        "consistency_max_day_pct": _coerce_float(bg.get("consistency_max_day_pct", 20), 20.0),
        "min_profitable_day_pct": _coerce_float(bg.get("min_profitable_day_pct", 0.5), 0.5),
        "min_profitable_days": _coerce_int(bg.get("min_profitable_days", 5), 5),
        "risk_per_trade_usd": _coerce_float(bg.get("risk_per_trade_usd", 25), 25.0),
        "kelly_cap_enabled": bool(bg.get("kelly_cap_enabled", True)),
        "culturing_loss_veto": bool(bg.get("culturing_loss_veto", True)),
        "per_symbol_max_lot": dict(bg.get("per_symbol_max_lot") or {}),
        "use_growth_gates": bool(bg.get("use_growth_gates", True)),
        "avoid_news": bool(bg.get("avoid_news", False)),
    }


def prepare_blue_guardian_profile(config: dict[str, Any]) -> dict[str, Any]:
    """Disable the $50k performance plan so funded eval uses growth-style entry gates."""
    if not blue_guardian_enabled(config):
        return config
    config.setdefault("performance", {})["apply_when"] = "never"
    # Blue Guardian tracks its own daily P&L — do not arm practice.growth campaign on real.
    config.setdefault("practice", {}).setdefault("growth", {})["enabled"] = False
    return config


def total_floating_pnl(positions: list[dict[str, Any]]) -> float:
    return round(sum(float(p.get("profit", 0) or 0) for p in positions), 2)


def count_open_positions(positions: list[dict[str, Any]]) -> int:
    return len(positions)


def symbol_position_count(positions: list[dict[str, Any]], symbol: str) -> int:
    return sum(1 for p in positions if p.get("symbol") == symbol)


def _load_open_times() -> dict[str, str]:
    data = read_json_state(OPEN_TIMES_FILE, default={}) or {}
    return data if isinstance(data, dict) else {}


def record_position_open(ticket: int | str, opened_at: str | None = None) -> None:
    times = _load_open_times()
    times[str(ticket)] = opened_at or utc_now_iso()
    write_json_state(OPEN_TIMES_FILE, times)


def prune_open_times(active_tickets: set[str]) -> None:
    times = _load_open_times()
    if not times:
        return
    pruned = {k: v for k, v in times.items() if k in active_tickets}
    if pruned != times:
        write_json_state(OPEN_TIMES_FILE, pruned)


def position_age_seconds(position: dict[str, Any], open_times: dict[str, str] | None = None) -> float:
    open_times = open_times or _load_open_times()
    ticket = str(position.get("ticket") or position.get("position_id") or "")
    # Agent-recorded UTC open time is authoritative; MT5 pos.time is broker-local
    # and is often mislabeled as UTC (blocks SL mods via min_hold forever).
    raw = open_times.get(ticket) or position.get("opened_at")
    if not raw:
        return 0.0
    try:
        if isinstance(raw, (int, float)):
            opened = datetime.fromtimestamp(float(raw), tz=timezone.utc)
        else:
            opened = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - opened).total_seconds())
    except (TypeError, ValueError, OSError):
        return 0.0


def is_emergency_close_reason(reason: str) -> bool:
    """All Blue Guardian risk closes bypass the 130s min-hold (firm tick-scalping exempt)."""
    return str(reason).startswith("blue_guardian_")


def can_close_position(
    config: dict[str, Any],
    position: dict[str, Any],
    *,
    reason: str = "",
    open_times: dict[str, str] | None = None,
) -> tuple[bool, str | None]:
    if not blue_guardian_enabled(config):
        return True, None
    if is_emergency_close_reason(reason):
        return True, None
    settings = blue_guardian_settings(config)
    age = position_age_seconds(position, open_times)
    if age >= settings["min_hold_seconds"]:
        return True, None
    remaining = int(settings["min_hold_seconds"] - age)
    return False, f"min_hold:{remaining}s_remaining"


def can_modify_position_sl(config: dict[str, Any], position: dict[str, Any]) -> bool:
    if not blue_guardian_enabled(config):
        return True
    settings = blue_guardian_settings(config)
    return position_age_seconds(position) >= settings["min_hold_seconds"]


def entry_gates(
    config: dict[str, Any],
    positions: list[dict[str, Any]],
    signal: dict[str, Any] | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    if not blue_guardian_enabled(config):
        return True, None, {"enabled": False}

    settings = blue_guardian_settings(config)
    state = read_json_state(STATE_FILE, default={}) or {}
    details: dict[str, Any] = {
        "enabled": True,
        "open_positions": count_open_positions(positions),
        "floating_pnl": total_floating_pnl(positions),
        "max_total": settings["max_total_open_positions"],
        "max_per_symbol": settings["max_open_per_symbol"],
    }

    if state.get("trading_paused"):
        return False, "blue_guardian_daily_pause", {**details, "pause_reason": state.get("pause_reason")}

    if count_open_positions(positions) >= settings["max_total_open_positions"]:
        return False, "blue_guardian_max_total", details

    floating = total_floating_pnl(positions)
    if floating <= settings["floating_block_new_trades_usd"]:
        return False, "blue_guardian_floating_block", {**details, "floating_pnl": floating}

    if signal:
        symbol = signal.get("symbol", "")
        if symbol_position_count(positions, symbol) >= settings["max_open_per_symbol"]:
            return False, "blue_guardian_symbol_full", {**details, "symbol": symbol}

    return True, None, details


def excess_position_close_actions(
    config: dict[str, Any],
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Close positions over max_total_open_positions (firm cap enforcement)."""
    if not blue_guardian_enabled(config):
        return []
    settings = blue_guardian_settings(config)
    max_total = settings["max_total_open_positions"]
    excess = len(positions) - max_total
    if excess <= 0:
        return []
    ranked = sorted(
        positions,
        key=lambda p: (
            float(p.get("profit", 0) or 0),
            position_age_seconds(p),
        ),
    )
    actions: list[dict[str, Any]] = []
    for pos in ranked[:excess]:
        actions.append({
            "ticket": pos.get("ticket"),
            "symbol": pos.get("symbol"),
            "side": pos.get("side"),
            "volume": pos.get("size"),
            "profit": float(pos.get("profit", 0) or 0),
            "reason": "blue_guardian_excess_positions",
            "emergency": True,
            "opened_at": pos.get("opened_at"),
        })
    return actions


def per_trade_close_actions(
    config: dict[str, Any],
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not blue_guardian_enabled(config):
        return []
    settings = blue_guardian_settings(config)
    actions: list[dict[str, Any]] = []
    for pos in positions:
        profit = float(pos.get("profit", 0) or 0)
        if profit <= -settings["per_trade_emergency_usd"]:
            reason = "blue_guardian_emergency_single"
        elif profit <= -settings["per_trade_hard_loss_usd"]:
            reason = "blue_guardian_hard_loss"
        elif profit <= -settings["per_trade_normal_loss_usd"]:
            reason = "blue_guardian_normal_loss"
        else:
            continue
        actions.append({
            "ticket": pos.get("ticket"),
            "symbol": pos.get("symbol"),
            "side": pos.get("side"),
            "volume": pos.get("size"),
            "profit": profit,
            "reason": reason,
            "emergency": "emergency" in reason,
        })
    return actions


def portfolio_close_all(
    config: dict[str, Any],
    positions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not blue_guardian_enabled(config) or not positions:
        return None
    settings = blue_guardian_settings(config)
    floating = total_floating_pnl(positions)
    if floating > settings["floating_close_all_usd"]:
        return None
    return {
        "reason": "blue_guardian_portfolio_flatten",
        "floating_pnl": floating,
        "threshold": settings["floating_close_all_usd"],
        "guardian_shield_usd": settings["guardian_shield_usd"],
        "position_count": len(positions),
    }


def _est_trading_day(hour_est: int) -> str:
    now = datetime.now(_EST)
    if now.hour < hour_est:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def _sync_high_watermark(closed_balance: float) -> dict[str, Any]:
    state = read_json_state(HIGH_WATERMARK_FILE, default={}) or {}
    prev = float(state.get("high_watermark_balance", 0) or 0)
    hw = max(prev, closed_balance)
    out = {"high_watermark_balance": round(hw, 2), "updated_at": utc_now_iso()}
    write_json_state(HIGH_WATERMARK_FILE, out)
    return out


def evaluate_daily_state(
    config: dict[str, Any],
    *,
    balance: float,
    equity: float,
) -> dict[str, Any]:
    if not blue_guardian_enabled(config):
        return {"enabled": False}

    settings = blue_guardian_settings(config)
    day = _est_trading_day(settings["daily_reset_hour_est"])
    prev = read_json_state(STATE_FILE, default={}) or {}
    size = settings["account_size_usd"]
    baseline = read_json_state("mt5_baseline.json", default={}) or {}
    baseline_login = str(baseline.get("login") or "")
    baseline_cash = float(baseline.get("starting_cash", 0) or 0)
    account_changed = bool(
        baseline_login
        and str(prev.get("baseline_login") or "") != baseline_login
    )

    if account_changed:
        anchor = baseline_cash or max(balance, equity)
        write_json_state(
            HIGH_WATERMARK_FILE,
            {"high_watermark_balance": round(anchor, 2), "updated_at": utc_now_iso()},
        )
        prev = {
            "day": day,
            "day_anchor_equity": round(anchor, 2),
            "baseline_login": baseline_login,
            "profitable_days": [],
            "guardian_shield_breaches": 0,
            "trading_paused": False,
            "pause_reason": None,
            "target_hit": False,
        }
    elif prev.get("day") != day:
        anchor = max(balance, equity)
        prev = {
            "day": day,
            "day_anchor_equity": round(anchor, 2),
            "baseline_login": baseline_login or prev.get("baseline_login"),
            "profitable_days": list(prev.get("profitable_days", [])),
            "guardian_shield_breaches": int(prev.get("guardian_shield_breaches", 0) or 0),
            "trading_paused": False,
            "pause_reason": None,
            "target_hit": False,
        }

    anchor = float(prev.get("day_anchor_equity", max(balance, equity)) or max(balance, equity))
    daily_pnl = round(equity - anchor, 2)
    daily_loss_limit = anchor - settings["max_daily_loss_usd"]
    daily_loss_buffer = anchor - settings["daily_loss_buffer_usd"]
    target_usd = settings["daily_profit_target_usd"]

    hw = _sync_high_watermark(balance)
    hw_bal = float(hw["high_watermark_balance"])
    trailing_floor = hw_bal * (1 - settings["max_trailing_drawdown_pct"] / 100.0)
    if hw_bal >= size * 1.06:
        trailing_floor = max(trailing_floor, size)

    trading_paused = False
    pause_reason = None
    target_hit = daily_pnl >= target_usd

    if equity <= daily_loss_buffer:
        trading_paused = True
        pause_reason = f"Blue Guardian daily buffer -${settings['daily_loss_buffer_usd']:.0f} hit"
    elif equity <= daily_loss_limit:
        trading_paused = True
        pause_reason = f"Blue Guardian max daily loss -${settings['max_daily_loss_usd']:.0f} hit"
    elif equity <= trailing_floor:
        trading_paused = True
        pause_reason = f"Blue Guardian trailing {settings['max_trailing_drawdown_pct']:.0f}% DD hit"
    elif target_hit and settings["daily_profit_lock_on_hit"]:
        trading_paused = True
        pause_reason = f"Blue Guardian +{settings['daily_profit_target_pct']:.0f}% daily target hit"

    profitable_days = list(prev.get("profitable_days", []))
    min_day = size * settings["min_profitable_day_pct"] / 100.0
    if daily_pnl >= min_day and day not in profitable_days:
        profitable_days.append(day)

    out = {
        "enabled": True,
        "day": day,
        "day_anchor_equity": round(anchor, 2),
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": round((daily_pnl / size) * 100, 2) if size else 0.0,
        "daily_profit_target_pct": settings["daily_profit_target_pct"],
        "daily_profit_target_usd": target_usd,
        "daily_loss_limit_equity": round(daily_loss_limit, 2),
        "daily_loss_buffer_equity": round(daily_loss_buffer, 2),
        "target_hit": target_hit,
        "trading_paused": trading_paused,
        "pause_reason": pause_reason,
        "high_watermark_balance": hw_bal,
        "trailing_drawdown_floor": round(trailing_floor, 2),
        "profitable_days_count": len(profitable_days),
        "profitable_days_required": settings["min_profitable_days"],
        "guardian_shield_usd": settings["guardian_shield_usd"],
        "floating_close_all_usd": settings["floating_close_all_usd"],
        "floating_block_new_usd": settings["floating_block_new_trades_usd"],
        "min_hold_seconds": settings["min_hold_seconds"],
        "risk_per_trade_usd": settings["risk_per_trade_usd"],
        "max_total_open_positions": settings["max_total_open_positions"],
        "max_open_per_symbol": settings["max_open_per_symbol"],
        "updated_at": utc_now_iso(),
    }
    write_json_state(STATE_FILE, out)
    return out


def risk_per_trade_cap(config: dict[str, Any]) -> float | None:
    from core.risk_cap import risk_per_trade_cap as _cap
    return _cap(config)


def max_lot_for_symbol(config: dict[str, Any], symbol: str, broker_max: float) -> float:
    if not blue_guardian_enabled(config):
        return broker_max
    caps = blue_guardian_settings(config)["per_symbol_max_lot"]
    sym_cap = caps.get(symbol)
    if sym_cap is None:
        return broker_max
    return min(float(sym_cap), broker_max)


def apply_config_overrides(config: dict[str, Any]) -> dict[str, Any]:
    if not blue_guardian_enabled(config):
        return config
    settings = blue_guardian_settings(config)
    # Persist resolved ints so downstream code never sees YAML "auto".
    bg_cfg = config.setdefault("blue_guardian", {})
    bg_cfg["max_total_open_positions"] = settings["max_total_open_positions"]
    growth = (config.get("practice") or {}).get("growth") or {}
    trading = config.setdefault("trading", {})
    trading["max_open_per_symbol"] = settings["max_open_per_symbol"]
    trading["allow_pyramiding"] = settings["allow_pyramiding"]
    if not settings["allow_pyramiding"]:
        trading["regime_flip_replace_enabled"] = False
    risk = config.setdefault("risk", {})
    risk["max_drawdown_pct"] = settings["max_trailing_drawdown_pct"]
    signals = config.setdefault("signals", {})
    pct = round(settings["risk_per_trade_usd"] / settings["account_size_usd"] * 100, 4)
    signals["default_risk_percent"] = pct
    kelly = signals.setdefault("kelly_sizing", {})
    if settings["kelly_cap_enabled"]:
        kelly["max_fraction"] = pct
        kelly["fallback_fraction"] = pct
    practice = config.setdefault("practice", {}).setdefault("growth", {})
    practice["daily_target_pct"] = settings["daily_profit_target_pct"]
    practice["max_daily_loss_pct"] = 3
    practice["risk_percent_per_trade"] = pct

    config.setdefault("execution", {})["starting_cash"] = settings["account_size_usd"]
    filters = config.setdefault("filters", {})
    filters["avoid_news"] = settings["avoid_news"]

    if settings["use_growth_gates"]:
        session = config.setdefault("session_scoring", {})
        quant = config.setdefault("quant", {})
        intel = config.setdefault("intelligence", {})
        session["enabled"] = bool(growth.get("session_scoring_enabled", False))
        session["min_trade_score"] = int(growth.get("min_trade_score", 28))
        session["min_trade_score_aggressive"] = int(
            growth.get("min_trade_score_aggressive", session["min_trade_score"])
        )
        signals["min_confidence"] = int(growth.get("min_confidence", 52))
        signals["min_risk_reward"] = float(growth.get("min_risk_reward", 0.95))
        filters["min_volume_ratio"] = float(growth.get("min_volume_ratio", 0.12))
        filters["spread_mult"] = float(growth.get("spread_mult", 4.0))
        risk["max_consecutive_losses"] = int(growth.get("max_consecutive_losses", 0))
        equity = settings["account_size_usd"]
        exposure_frac = float(growth.get("max_exposure_fraction", 0.92))
        cap = round(equity * exposure_frac, 2)
        risk["max_symbol_exposure_usd"] = cap
        risk["max_total_exposure_usd"] = cap
        quant["strategy_ranking_enabled"] = bool(growth.get("strategy_ranking_enabled", False))
        quant["require_top_ranked_setup"] = False
        trading["aggressive_mode"] = bool(growth.get("aggressive_mode", True))
        trading["max_session_trades_per_symbol"] = int(
            growth.get("max_session_trades_per_symbol", 24)
        )
        if growth.get("dynamic_entries_enabled"):
            trading.setdefault("dynamic_entries", {})["enabled"] = True
        if growth.get("relax_consensus", True):
            intel["regime_veto_enabled"] = False
            intel["trend_structure_veto_enabled"] = False
            intel["risk_veto_threshold"] = int(growth.get("risk_veto_threshold", 6))
        config.setdefault("practice", {})["relax_structure_checks"] = True
        config.setdefault("practice", {})["relax_volume_check"] = True
    return config


def cell_loss_stats(trades: list[dict[str, Any]]) -> dict[str, float | int]:
    losses = [float(t.get("pnl", 0)) for t in trades if float(t.get("pnl", 0) or 0) < 0]
    if not losses:
        return {
            "avg_loss_usd": 0.0,
            "max_loss_usd": 0.0,
            "breach_25_count": 0,
            "breach_30_count": 0,
        }
    breach_25 = sum(1 for x in losses if x <= -25)
    breach_30 = sum(1 for x in losses if x <= -30)
    return {
        "avg_loss_usd": round(sum(losses) / len(losses), 2),
        "max_loss_usd": round(min(losses), 2),
        "breach_25_count": breach_25,
        "breach_30_count": breach_30,
    }
