"""Trade manager — live watch, stale close, param logging, ghost upgrades.

Philosophy (user 2026-07-15):
  * Tight BE/trail is a primary payoff killer — prefer wider, distance-based exits.
  * Strategy choice is *scenario-fit* (which recipe fits this symbol *now*), not a
    global "best strategy" crown.
  * The trade manager is part of the main pipeline: watches open trades, logs every
    indicator parameter used, closes stale losers, and runs *ghost* upgrades
    (virtual BE/trail/SL recipes). Ghosts that beat live replace the original.

Ghosts never send MT5 orders. They re-score closed trades under alternate
management recipes using MFE/MAE (same optimistic-but-honest family as
``calibrate_be_trail``). Promotion requires n≥min_n, meanΔR margin, and CI-ish
stability (majority of last N closes better under ghost).
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from core.utils import read_json_state, utc_now_iso, write_json_state

STATE_FILE = "trade_manager.json"
GHOST_LEDGER = "trade_manager_ghost_ledger.json"
PROMOTIONS_FILE = "trade_manager_promotions.json"
PARAM_LOG = "trade_manager_param_log.jsonl"

# Feature keys logged with every open/close for later ghost experiments.
INDICATOR_KEYS: tuple[str, ...] = (
    "price",
    "atr",
    "atr_m15",
    "atr_h1",
    "rsi",
    "rsi_m15",
    "ema_fast",
    "ema_slow",
    "ema_slope",
    "ema50",
    "ema200",
    "bb_upper",
    "bb_lower",
    "bb_width",
    "volume_ratio",
    "relative_volume",
    "spread_points",
    "adx",
    "macd",
    "macd_signal",
    "stoch_k",
    "stoch_d",
    "session_score",
)

# Named ghost upgrade presets (points-first where possible).
# XAU point ≈ 0.01 → 5000 pts = $50 price, 3000 pts lock ≈ $30 (user request).
GHOST_PRESETS: dict[str, dict[str, Any]] = {
    "wide_points": {
        "label": "wide_points_BE5000_lock3000",
        "break_even": {
            "trigger_profit_usd": None,  # distance-only; kill early $3 BE
            "lock_profit_usd": None,
            "trigger_points": 5000,
            "lock_profit_points": 3000,
            "trigger_atr_mult": 1.0,
            "lock_profit_atr_mult": 0.35,
        },
        "trailing": {
            "enabled": True,
            "activation_profit_usd": None,
            "activation_points": 5500,
            "activation_atr_mult": 1.2,
            "trail_points_atr_mult": 0.85,
            "trail_atr_mult": 0.55,
        },
        "management_r": {
            "break_even_trigger_r": 0.85,
            "break_even_lock_r": 0.25,
            "trail_start_r": 1.15,
            "trail_atr_mult": 0.55,
            "tp1_r": 1.35,
            "tp2_r": 2.0,
        },
    },
    "medium_wide": {
        "label": "medium_wide_BE0.7R",
        "break_even": {
            "trigger_profit_usd": None,
            "lock_profit_usd": None,
            "trigger_atr_mult": 0.7,
            "lock_profit_atr_mult": 0.2,
        },
        "trailing": {
            "enabled": True,
            "activation_profit_usd": None,
            "activation_atr_mult": 0.95,
            "trail_points_atr_mult": 0.65,
            "trail_atr_mult": 0.45,
        },
        "management_r": {
            "break_even_trigger_r": 0.7,
            "break_even_lock_r": 0.15,
            "trail_start_r": 0.95,
            "trail_atr_mult": 0.45,
            "tp1_r": 1.15,
            "tp2_r": 1.6,
        },
    },
    "tight_control": {
        "label": "legacy_tight_control",
        "break_even": {
            "trigger_profit_usd": 3,
            "lock_profit_usd": 2,
            "trigger_atr_mult": 0.4,
            "lock_profit_atr_mult": 0.08,
        },
        "trailing": {
            "enabled": True,
            "activation_profit_usd": 3,
            "activation_atr_mult": 0.55,
            "trail_points_atr_mult": 0.28,
            "trail_atr_mult": 0.28,
        },
        "management_r": {
            "break_even_trigger_r": 0.4,
            "break_even_lock_r": 0.05,
            "trail_start_r": 0.55,
            "trail_atr_mult": 0.28,
            "tp1_r": 0.9,
            "tp2_r": 1.2,
        },
    },
}


def trade_manager_cfg(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("trade_manager") or {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "ghost_enabled": bool(raw.get("ghost_enabled", True)),
        "promote_enabled": bool(raw.get("promote_enabled", True)),
        "min_n_promote": int(raw.get("min_n_promote", 12)),
        "min_delta_r": float(raw.get("min_delta_r", 0.08)),
        "majority_frac": float(raw.get("majority_frac", 0.55)),
        "stale_close_enabled": bool(raw.get("stale_close_enabled", True)),
        "stale_close_minutes": float(
            raw.get("stale_close_minutes")
            or (config.get("risk") or {}).get("stale_close_minutes")
            or (config.get("risk") or {}).get("max_order_age_minutes")
            or 30
        ),
        "stale_only_if_losing": bool(raw.get("stale_only_if_losing", True)),
        "active_ghosts": list(
            raw.get("active_ghosts")
            or ["wide_points", "medium_wide", "tight_control"]
        ),
        "log_indicators": bool(raw.get("log_indicators", True)),
        # Prefer distance (points/ATR) over tiny USD on micro accounts.
        "distance_first_triggers": bool(raw.get("distance_first_triggers", True)),
    }


def snapshot_indicators(feat: dict[str, Any] | None) -> dict[str, Any]:
    """Extract indicator parameters used for the setup (for ghost experiments)."""
    feat = feat or {}
    out: dict[str, Any] = {}
    for k in INDICATOR_KEYS:
        if k in feat and feat[k] is not None:
            try:
                out[k] = float(feat[k]) if isinstance(feat[k], (int, float)) else feat[k]
            except (TypeError, ValueError):
                out[k] = feat[k]
    # Nested common blocks
    for block in ("indicators", "oscillators", "structure"):
        nested = feat.get(block)
        if isinstance(nested, dict):
            for k, v in nested.items():
                if k not in out and v is not None:
                    out[f"{block}.{k}"] = v
    return out


def scenario_key(
    symbol: str,
    setup: str | None,
    regime: str | None,
    session: str | None,
) -> str:
    """Canonical scenario cell: symbol|setup|regime|session."""
    return "|".join(
        [
            str(symbol or "?"),
            str(setup or "unknown"),
            str(regime or "unknown"),
            str(session or "unknown"),
        ]
    )


def _regime_from_ctx(ctx: dict[str, Any] | None) -> str:
    ctx = ctx or {}
    mr = ctx.get("market_regime") if isinstance(ctx.get("market_regime"), dict) else {}
    return str(
        (mr or {}).get("primary")
        or ctx.get("regime")
        or "unknown"
    )


def scenario_from_signal(signal: dict[str, Any]) -> dict[str, str]:
    ctx = signal.get("market_context") if isinstance(signal.get("market_context"), dict) else {}
    setup = str(signal.get("setup_type") or signal.get("setup") or "unknown")
    session = str(ctx.get("session") or "unknown")
    regime = _regime_from_ctx(ctx)
    symbol = str(signal.get("symbol") or "?")
    return {
        "symbol": symbol,
        "setup": setup,
        "regime": regime,
        "session": session,
        "key": scenario_key(symbol, setup, regime, session),
    }


def _load_promotions() -> dict[str, Any]:
    doc = read_json_state(PROMOTIONS_FILE, default={}) or {}
    return doc if isinstance(doc, dict) else {}


def _save_promotions(doc: dict[str, Any]) -> None:
    write_json_state(PROMOTIONS_FILE, doc)


def _load_ghost_ledger() -> dict[str, Any]:
    doc = read_json_state(GHOST_LEDGER, default={"cells": {}}) or {}
    if not isinstance(doc, dict):
        doc = {"cells": {}}
    doc.setdefault("cells", {})
    return doc


def _save_ghost_ledger(doc: dict[str, Any]) -> None:
    write_json_state(GHOST_LEDGER, doc)


def active_promotion(symbol: str) -> dict[str, Any] | None:
    """Return trusted promotion for symbol, if any."""
    doc = _load_promotions()
    row = (doc.get("symbols") or {}).get(symbol)
    if not isinstance(row, dict):
        return None
    if not row.get("trusted"):
        return None
    return row


def scenario_management_recipe(
    signal: dict[str, Any],
    config: dict[str, Any],
    *,
    session_bias: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pick management + sizing knobs that fit *this* scenario for *this* symbol.

    Priority:
      1. Trusted trade_manager promotion for the symbol
      2. session_bias / best_policies (caller may already merge)
      3. Regime template (trending → wider trail; range → tighter TP, later BE)
      4. Wide defaults (anti-tight-BE)
    """
    from core.policy_score import session_entry_bias

    scen = scenario_from_signal(signal)
    session = scen["session"]
    regime = scen["regime"]
    bias = dict(session_bias or session_entry_bias(session))

    # Regime templates — fit the scenario, not a global winner.
    reg = (regime or "").lower()
    if "trend" in reg or reg in ("strong_trend", "weak_trend", "trending"):
        bias.setdefault("break_even_trigger_r", 0.85)
        bias.setdefault("trail_start_r", 1.1)
        bias.setdefault("trail_atr_mult", 0.55)
        bias.setdefault("tp1_r", 1.35)
        bias.setdefault("tp2_r", 2.0)
        bias.setdefault("sl_atr_mult", 1.25)
        bias.setdefault("max_hold_minutes", 45)
    elif "range" in reg or "compress" in reg or "mean" in reg:
        bias.setdefault("break_even_trigger_r", 0.65)
        bias.setdefault("trail_start_r", 0.9)
        bias.setdefault("trail_atr_mult", 0.4)
        bias.setdefault("tp1_r", 0.95)
        bias.setdefault("tp2_r", 1.25)
        bias.setdefault("sl_atr_mult", 1.05)
        bias.setdefault("max_hold_minutes", 25)
    else:
        # Default: wider than legacy tight 0.4R BE
        bias.setdefault("break_even_trigger_r", 0.75)
        bias.setdefault("trail_start_r", 1.0)
        bias.setdefault("trail_atr_mult", 0.5)
        bias.setdefault("tp1_r", 1.2)
        bias.setdefault("tp2_r", 1.7)
        bias.setdefault("sl_atr_mult", 1.15)
        bias.setdefault("max_hold_minutes", 35)

    promo = active_promotion(scen["symbol"])
    source = "scenario_template"
    if promo and isinstance(promo.get("management_r"), dict):
        for k, v in promo["management_r"].items():
            if v is not None:
                bias[k] = v
        source = f"promotion:{promo.get('ghost_id', 'unknown')}"

    recipe = {
        "scenario_key": scen["key"],
        "symbol": scen["symbol"],
        "setup": scen["setup"],
        "regime": regime,
        "session": session,
        "source": source,
        "entry_type": bias.get("entry_type", "limit"),
        "limit_offset_atr": float(bias.get("limit_offset_atr") or 0.1),
        "sl_model": "structure_atr",
        "sl_atr_mult": float(bias.get("sl_atr_mult") or 1.15),
        "tp_model": "rr",
        "tp1_r": float(bias.get("tp1_r") or 1.2),
        "tp2_r": float(bias.get("tp2_r") or 1.7),
        "break_even_enabled": True,
        "break_even_trigger_r": float(bias.get("break_even_trigger_r") or 0.75),
        "break_even_lock_r": float(bias.get("break_even_lock_r") or 0.2),
        "trailing_enabled": True,
        "trail_start_r": float(bias.get("trail_start_r") or 1.0),
        "trail_atr_mult": float(bias.get("trail_atr_mult") or 0.5),
        "max_hold_minutes": int(bias.get("max_hold_minutes") or 35),
        "cancel_if_not_filled_seconds": int(bias.get("cancel_if_not_filled_seconds") or 110),
        # Points-based override hints for position_manager (XAU-style wide).
        "trigger_points": bias.get("trigger_points"),
        "lock_profit_points": bias.get("lock_profit_points"),
        "activation_points": bias.get("activation_points"),
    }

    # Merge promotion point-level BE/trail if present
    if promo:
        be = promo.get("break_even") if isinstance(promo.get("break_even"), dict) else {}
        tr = promo.get("trailing") if isinstance(promo.get("trailing"), dict) else {}
        for k in ("trigger_points", "lock_profit_points", "trigger_atr_mult", "lock_profit_atr_mult"):
            if be.get(k) is not None:
                recipe[k] = be[k]
        for k in ("activation_points", "activation_atr_mult", "trail_points_atr_mult", "trail_atr_mult"):
            if tr.get(k) is not None:
                recipe[k] = tr[k]
        recipe["promotion_ghost_id"] = promo.get("ghost_id")

    return recipe


def merge_promotion_into_mgmt(
    be_sym: dict[str, Any],
    trail_sym: dict[str, Any],
    symbol: str,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Apply trusted trade_manager promotion over per-symbol BE/trail dicts."""
    promo = active_promotion(symbol)
    if not promo:
        return be_sym, trail_sym, False
    applied = False
    be = promo.get("break_even") if isinstance(promo.get("break_even"), dict) else None
    tr = promo.get("trailing") if isinstance(promo.get("trailing"), dict) else None
    if be:
        be_sym = dict(be_sym)
        for k, v in be.items():
            if v is not None:
                be_sym[k] = v
                applied = True
        # Kill early USD triggers when promotion is distance-first
        if be.get("trigger_profit_usd") is None and "trigger_points" in be:
            be_sym["trigger_profit_usd"] = None
        if be.get("lock_profit_usd") is None and "lock_profit_points" in be:
            be_sym["lock_profit_usd"] = None
    if tr:
        trail_sym = dict(trail_sym)
        for k, v in tr.items():
            if v is not None:
                trail_sym[k] = v
                applied = True
        if tr.get("enabled") is not None:
            trail_sym["enabled"] = tr["enabled"]
    return be_sym, trail_sym, applied


def _ghost_exit_r(
    mae_r: float,
    mfe_r: float,
    realized_r: float | None,
    tp1_r: float | None,
    *,
    be_trig: float,
    be_lock: float,
    tr_act: float,
    tr_dist: float,
) -> float:
    """Optimistic path model (favorable peak before adverse) — same family as calibrate_be_trail."""
    if tp1_r is not None and mfe_r >= tp1_r:
        return float(tp1_r)
    if mae_r >= 1.0:
        return be_lock if mfe_r >= be_trig else -1.0
    if mfe_r >= tr_act:
        return max(mfe_r - tr_dist, -1.0)
    if mfe_r >= be_trig:
        return be_lock
    return float(realized_r) if realized_r is not None else 0.0


def score_trade_under_ghost(
    trade: dict[str, Any],
    ghost: dict[str, Any],
) -> float | None:
    """Return virtual R for one closed trade under a ghost management recipe."""
    entry = trade.get("entry")
    sl = trade.get("sl_initial") or trade.get("sl")
    mae = trade.get("mae_R")
    mfe = trade.get("mfe_R")
    if entry is None or sl is None:
        # Fall back: compare realized only — no path data
        rm = trade.get("r_multiple")
        try:
            return float(rm) if rm is not None else None
        except (TypeError, ValueError):
            return None
    try:
        entry_f = float(entry)
        sl_f = float(sl)
        risk = abs(entry_f - sl_f)
        if risk <= 0:
            return None
    except (TypeError, ValueError):
        return None

    try:
        mae_r = float(mae) if mae is not None else None
        mfe_r = float(mfe) if mfe is not None else None
    except (TypeError, ValueError):
        mae_r = mfe_r = None

    # If MAE/MFE missing, cannot path-sim; skip ghost path score
    if mae_r is None or mfe_r is None:
        return None

    mgmt = ghost.get("management_r") or {}
    be_trig = float(mgmt.get("break_even_trigger_r") or 0.75)
    be_lock = float(mgmt.get("break_even_lock_r") or 0.2)
    tr_act = float(mgmt.get("trail_start_r") or 1.0)
    tr_dist = float(mgmt.get("trail_atr_mult") or 0.5)
    tp1 = trade.get("tp1")
    tp1_r = None
    if tp1 is not None:
        try:
            tp1_r = abs(float(tp1) - entry_f) / risk
        except (TypeError, ValueError):
            tp1_r = float(mgmt.get("tp1_r") or 1.2)
    else:
        tp1_r = float(mgmt.get("tp1_r") or 1.2)

    realized = trade.get("r_multiple")
    try:
        realized_f = float(realized) if realized is not None else None
    except (TypeError, ValueError):
        realized_f = None

    return _ghost_exit_r(
        mae_r, mfe_r, realized_f, tp1_r,
        be_trig=be_trig, be_lock=be_lock, tr_act=tr_act, tr_dist=tr_dist,
    )


def record_ghost_outcomes(
    trades: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Score recent closes under each ghost; update ledger; maybe promote."""
    log = logger or logging.getLogger("trade_manager")
    cfg = trade_manager_cfg(config)
    if not cfg["ghost_enabled"]:
        return {"ghosts": 0, "promoted": []}

    ledger = _load_ghost_ledger()
    cells: dict[str, Any] = ledger.setdefault("cells", {})
    ghost_ids = [g for g in cfg["active_ghosts"] if g in GHOST_PRESETS]
    updated = 0

    for trade in trades:
        if trade.get("archive_polluted"):
            continue
        tid = str(trade.get("trade_id") or trade.get("mt5_deal") or trade.get("ticket") or "")
        if not tid:
            continue
        symbol = str(trade.get("symbol") or "")
        if not symbol:
            continue
        # Live realized R (baseline)
        try:
            live_r = float(trade["r_multiple"]) if trade.get("r_multiple") is not None else None
        except (TypeError, ValueError):
            live_r = None
        if live_r is None:
            # Approximate from pnl if risk known
            continue

        for gid in ghost_ids:
            ghost = GHOST_PRESETS[gid]
            ghost_r = score_trade_under_ghost(trade, ghost)
            if ghost_r is None:
                continue
            cell_key = f"{symbol}|{gid}"
            cell = cells.setdefault(
                cell_key,
                {
                    "symbol": symbol,
                    "ghost_id": gid,
                    "n": 0,
                    "sum_live_r": 0.0,
                    "sum_ghost_r": 0.0,
                    "sum_delta_r": 0.0,
                    "wins_vs_live": 0,
                    "seen_trade_ids": [],
                },
            )
            seen = cell.setdefault("seen_trade_ids", [])
            if tid in seen:
                continue
            seen.append(tid)
            if len(seen) > 200:
                cell["seen_trade_ids"] = seen[-200:]
            cell["n"] = int(cell.get("n") or 0) + 1
            cell["sum_live_r"] = float(cell.get("sum_live_r") or 0) + live_r
            cell["sum_ghost_r"] = float(cell.get("sum_ghost_r") or 0) + float(ghost_r)
            delta = float(ghost_r) - live_r
            cell["sum_delta_r"] = float(cell.get("sum_delta_r") or 0) + delta
            if delta > 0:
                cell["wins_vs_live"] = int(cell.get("wins_vs_live") or 0) + 1
            cell["mean_live_r"] = cell["sum_live_r"] / cell["n"]
            cell["mean_ghost_r"] = cell["sum_ghost_r"] / cell["n"]
            cell["mean_delta_r"] = cell["sum_delta_r"] / cell["n"]
            cell["win_frac_vs_live"] = cell["wins_vs_live"] / cell["n"]
            cell["updated_at"] = utc_now_iso()
            updated += 1

    ledger["updated_at"] = utc_now_iso()
    _save_ghost_ledger(ledger)

    promoted: list[dict[str, Any]] = []
    if cfg["promote_enabled"]:
        promoted = _maybe_promote(ledger, cfg, logger=log)

    log.info(
        "Ghost ledger: scored %d trade×ghost, promoted=%d",
        updated,
        len(promoted),
    )
    return {"ghosts_scored": updated, "promoted": promoted, "cells": len(cells)}


def _maybe_promote(
    ledger: dict[str, Any],
    cfg: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    log = logger or logging.getLogger("trade_manager")
    min_n = cfg["min_n_promote"]
    min_delta = cfg["min_delta_r"]
    maj = cfg["majority_frac"]
    cells = ledger.get("cells") or {}
    # Best ghost per symbol
    best: dict[str, dict[str, Any]] = {}
    for cell in cells.values():
        if not isinstance(cell, dict):
            continue
        n = int(cell.get("n") or 0)
        if n < min_n:
            continue
        delta = float(cell.get("mean_delta_r") or 0)
        win_frac = float(cell.get("win_frac_vs_live") or 0)
        if delta < min_delta or win_frac < maj:
            continue
        # Ghost must itself be net positive
        if float(cell.get("mean_ghost_r") or 0) <= 0:
            continue
        sym = str(cell.get("symbol") or "")
        prev = best.get(sym)
        if prev is None or delta > float(prev.get("mean_delta_r") or 0):
            best[sym] = cell

    if not best:
        return []

    promo_doc = _load_promotions()
    symbols = promo_doc.setdefault("symbols", {})
    out: list[dict[str, Any]] = []
    for sym, cell in best.items():
        gid = str(cell.get("ghost_id"))
        preset = GHOST_PRESETS.get(gid)
        if not preset:
            continue
        existing = symbols.get(sym) or {}
        if (
            existing.get("trusted")
            and existing.get("ghost_id") == gid
            and float(existing.get("mean_delta_r") or 0) >= float(cell.get("mean_delta_r") or 0)
        ):
            continue  # already promoted this or better
        row = {
            "trusted": True,
            "ghost_id": gid,
            "label": preset.get("label"),
            "break_even": deepcopy(preset.get("break_even") or {}),
            "trailing": deepcopy(preset.get("trailing") or {}),
            "management_r": deepcopy(preset.get("management_r") or {}),
            "n": cell.get("n"),
            "mean_delta_r": cell.get("mean_delta_r"),
            "mean_ghost_r": cell.get("mean_ghost_r"),
            "mean_live_r": cell.get("mean_live_r"),
            "win_frac_vs_live": cell.get("win_frac_vs_live"),
            "promoted_at": utc_now_iso(),
            "source": "trade_manager_ghost",
        }
        symbols[sym] = row
        out.append({"symbol": sym, **row})
        log.info(
            "PROMOTE ghost %s → %s n=%s ΔR=%.3f ghost_meanR=%.3f",
            gid,
            sym,
            cell.get("n"),
            float(cell.get("mean_delta_r") or 0),
            float(cell.get("mean_ghost_r") or 0),
        )

    promo_doc["symbols"] = symbols
    promo_doc["updated_at"] = utc_now_iso()
    _save_promotions(promo_doc)
    # Mirror into symbol_be_trail_live so position_manager _merge_live also sees it
    _mirror_promotions_to_be_trail(promo_doc)
    return out


def _mirror_promotions_to_be_trail(promo_doc: dict[str, Any]) -> None:
    """Write trusted promotions into symbol_be_trail_live.json (same consume path)."""
    live = read_json_state("symbol_be_trail_live.json", default={}) or {}
    if not isinstance(live, dict):
        live = {}
    symbols = live.setdefault("symbols", {})
    for sym, row in (promo_doc.get("symbols") or {}).items():
        if not isinstance(row, dict) or not row.get("trusted"):
            continue
        symbols[sym] = {
            "trusted": True,
            "source": "trade_manager_promotion",
            "ghost_id": row.get("ghost_id"),
            "break_even": row.get("break_even") or {},
            "trailing": row.get("trailing") or {},
            "n": row.get("n"),
            "mean_delta_r": row.get("mean_delta_r"),
            "updated_at": utc_now_iso(),
        }
    live["updated_at"] = utc_now_iso()
    write_json_state("symbol_be_trail_live.json", live)


def log_open_position_params(
    positions: list[dict[str, Any]],
    features: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Snapshot indicator + management params for every open position."""
    cfg = trade_manager_cfg(config)
    if not cfg["log_indicators"]:
        return []
    rows: list[dict[str, Any]] = []
    feat_syms = (features or {}).get("symbols") or {}
    for pos in positions:
        symbol = str(pos.get("symbol") or "")
        feat = feat_syms.get(symbol) or {}
        meta = pos.get("signal_meta") if isinstance(pos.get("signal_meta"), dict) else {}
        mgmt = pos.get("management_profile") or meta.get("management_profile") or {}
        scen = scenario_from_signal({**pos, "market_context": pos.get("market_context") or meta.get("market_context")})
        row = {
            "ts": utc_now_iso(),
            "event": "open_watch",
            "ticket": pos.get("ticket") or pos.get("position_id"),
            "symbol": symbol,
            "side": pos.get("side"),
            "entry": pos.get("entry"),
            "sl": pos.get("sl"),
            "tp": pos.get("tp1") or pos.get("tp"),
            "profit": pos.get("profit"),
            "scenario": scen,
            "management_profile": mgmt,
            "indicators": snapshot_indicators(feat),
            "promotion": active_promotion(symbol),
        }
        rows.append(row)
    if rows:
        state = read_json_state(STATE_FILE, default={}) or {}
        if not isinstance(state, dict):
            state = {}
        hist = list(state.get("open_snapshots") or [])
        hist.extend(rows)
        state["open_snapshots"] = hist[-100:]
        state["last_watch"] = utc_now_iso()
        state["open_count"] = len(positions)
        write_json_state(STATE_FILE, state)
    return rows


def evaluate_stale_positions(
    positions: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    open_times: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return positions that should be closed as stale (losing + max hold)."""
    from core.blue_guardian import position_age_seconds

    cfg = trade_manager_cfg(config)
    if not cfg["stale_close_enabled"]:
        return []
    max_min = float(cfg["stale_close_minutes"])
    only_losing = cfg["stale_only_if_losing"]
    open_times = open_times or (read_json_state("position_open_times.json", default={}) or {})
    stale: list[dict[str, Any]] = []
    for pos in positions:
        age = position_age_seconds(pos, open_times if isinstance(open_times, dict) else {})
        if age < max_min * 60:
            continue
        profit = pos.get("profit")
        try:
            profit_f = float(profit) if profit is not None else None
        except (TypeError, ValueError):
            profit_f = None
        if only_losing and profit_f is not None and profit_f >= 0:
            continue
        # management profile can shorten hold
        mp = pos.get("management_profile") or {}
        try:
            mp_hold = float(mp.get("max_hold_minutes") or max_min)
        except (TypeError, ValueError):
            mp_hold = max_min
        if age < mp_hold * 60:
            continue
        stale.append({
            **pos,
            "_stale_age_sec": age,
            "_stale_reason": f"max_hold>{mp_hold:.0f}m",
        })
    return stale


def run_trade_manager_cycle(
    config: dict[str, Any],
    positions: list[dict[str, Any]],
    features: dict[str, Any],
    *,
    closed_trades: list[dict[str, Any]] | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Main trade-manager tick: watch, log, ghost-score, promote.

    Closing stale trades is still done by ``position_manager`` (has MT5 handle);
    this cycle *flags* them in state for the manager + dashboard.
    """
    log = logger or logging.getLogger("trade_manager")
    cfg = trade_manager_cfg(config)
    if not cfg["enabled"]:
        return {"enabled": False}

    snapshots = log_open_position_params(positions, features, config)
    stale = evaluate_stale_positions(positions, config)

    trades = closed_trades
    if trades is None:
        pt = read_json_state("paper_trades.json", default={}) or {}
        trades = list(pt.get("trades") or []) if isinstance(pt, dict) else []
    # Score only recent tail for speed
    recent = list(trades)[-80:]
    ghost_summary = record_ghost_outcomes(recent, config, logger=log)

    summary = {
        "enabled": True,
        "timestamp": utc_now_iso(),
        "open_positions": len(positions),
        "snapshots": len(snapshots),
        "stale_candidates": [
            {
                "ticket": p.get("ticket") or p.get("position_id"),
                "symbol": p.get("symbol"),
                "age_sec": p.get("_stale_age_sec"),
                "reason": p.get("_stale_reason"),
                "profit": p.get("profit"),
            }
            for p in stale
        ],
        "ghost": ghost_summary,
        "promotions": list((_load_promotions().get("symbols") or {}).keys()),
    }
    state = read_json_state(STATE_FILE, default={}) or {}
    if not isinstance(state, dict):
        state = {}
    state["last_cycle"] = summary
    state["stale_candidates"] = summary["stale_candidates"]
    write_json_state(STATE_FILE, state)

    if stale:
        log.info("Stale candidates: %d", len(stale))
    return summary
