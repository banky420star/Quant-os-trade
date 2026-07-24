"""Executable lot sizing — shared by verifier exposure checks and MT5 broker."""

from __future__ import annotations

from typing import Any

from core.blue_guardian import max_lot_for_symbol
from core.dynamic_entry import pyramid_layer_index, scale_lot_for_layer
from core.kelly_sizing import kelly_for_signal
from core.micro_profile import micro_profile_enabled
from core.risk_cap import (
    effective_risk_cap,
    estimate_stop_loss_usd,
    has_per_trade_cap,
    risk_per_trade_cap,
)


# Conservative fallbacks when MT5 symbol_info is unavailable (verifier / replay).
DEFAULT_SYMBOL_SPECS: dict[str, dict[str, float]] = {
    "XAUUSDm": {"volume_min": 0.01, "volume_step": 0.01, "trade_tick_value": 0.1, "trade_tick_size": 0.001, "point": 0.001},
    "USOILm": {"volume_min": 0.01, "volume_step": 0.01, "trade_tick_value": 1.0, "trade_tick_size": 0.01, "point": 0.01},
    "UK100m": {"volume_min": 0.01, "volume_step": 0.01, "trade_tick_value": 0.1, "trade_tick_size": 0.01, "point": 0.01},
}


def requires_executable_sizing(config: dict[str, Any]) -> bool:
    """True when verifier must mirror broker min-lot + risk-cap gates."""
    # Recognise a percent-of-equity cap too (priced later against live balance),
    # not only a static dollar cap — otherwise account-aware sizing wouldn't
    # trigger the executable-sizing path.
    if has_per_trade_cap(config):
        return True
    return micro_profile_enabled(config)


def symbol_spec_from_mt5(info: Any) -> dict[str, float]:
    return {
        "volume_min": float(getattr(info, "volume_min", 0) or 0.01),
        "volume_step": float(getattr(info, "volume_step", 0) or 0.01),
        "trade_tick_value": float(getattr(info, "trade_tick_value", 0) or 0),
        "trade_tick_size": float(getattr(info, "trade_tick_size", 0) or getattr(info, "point", 0) or 0),
        "point": float(getattr(info, "point", 0) or 0),
    }


def resolve_symbol_spec(
    symbol: str,
    config: dict[str, Any],
    *,
    overrides: dict[str, dict[str, float]] | None = None,
) -> dict[str, float]:
    if overrides and symbol in overrides:
        return dict(overrides[symbol])
    custom = (config.get("execution") or {}).get("symbol_specs") or {}
    if symbol in custom:
        base = dict(DEFAULT_SYMBOL_SPECS.get(symbol, DEFAULT_SYMBOL_SPECS["XAUUSDm"]))
        base.update(custom[symbol])
        return base
    return dict(DEFAULT_SYMBOL_SPECS.get(symbol, {"volume_min": 0.01, "volume_step": 0.01, "trade_tick_value": 0.0, "trade_tick_size": 0.0, "point": 0.0}))


def normalize_volume(volume: float, spec: dict[str, float]) -> float:
    step = float(spec.get("volume_step") or 0.01)
    vmin = float(spec.get("volume_min") or step)
    vmax = float(spec.get("volume_max") or 100.0)
    vol = max(vmin, min(vmax, volume))
    steps = round(vol / step)
    return round(steps * step, 2)


def calc_executable_volume(
    signal: dict[str, Any],
    *,
    equity: float,
    balance: float,
    config: dict[str, Any],
    symbol_spec: dict[str, float],
    open_positions: list[dict[str, Any]] | None = None,
    stamp_kelly: bool = False,
) -> tuple[float, dict[str, Any]]:
    """
    Mirror MT5 broker lot sizing including min-lot bump, exposure caps, and
    per-trade USD stop-loss cap. Returns (volume, details); volume 0 = not executable.
    """
    from core.exposure import (
        calc_risk_based_size,
        cap_size_to_exposure_limits,
        position_exposure_usd,
        position_notional,
    )

    positions = list(open_positions or [])
    exec_cfg = config.get("execution", {})
    default_risk_pct = float(config.get("signals", {}).get("default_risk_percent", 1))
    kelly = kelly_for_signal(signal, config, default_risk_pct)
    if stamp_kelly:
        signal["kelly"] = kelly
    risk_pct = float(kelly.get("fraction", default_risk_pct))

    # 2026-07-22 — symbol canary A/B: apply the canary lot multiplier at
    # the RISK_PCT layer (BEFORE the USD risk cap clamps risk_money) so
    # the boost flows through cap-normalisation. The previous post-clamp
    # IDEAL-amplification was silently cancelled by min_lot + USD-cap
    # `cap_size_to_exposure_limits`. Observe-only — solely the canary
    # state file is the persistent surface; no config mutation.
    try:
        from core.canary import lot_multiplier_for as _canary_mult
        _canary_boost = float(_canary_mult(symbol, config=config) or 1.0)
        _canary_boost = _canary_boost if _canary_boost > 1.0 else 1.0
    except Exception:
        _canary_boost = 1.0
    if _canary_boost > 1.0:
        risk_pct = risk_pct * _canary_boost
        details["canary_multiplier"] = round(_canary_boost, 3)

    # Risk-parity scaling: when the candidate carries a stream share (e.g.
    # donchian_breakout 0.25), we reduce per-trade risk_pct AND USD cap by
    # that share so the 4-stream bot stays within its risk budget.
    rp_share = signal.get("risk_parity_share")
    rp_active = rp_share is not None and 0 < float(rp_share) <= 1
    if rp_active:
        risk_pct = risk_pct * float(rp_share)

    max_lot = float(exec_cfg.get("max_lot", 0.1))
    default_lot = float(exec_cfg.get("default_lot", 0.01))
    symbol = signal["symbol"]
    entry = float(signal.get("entry", 0))
    sl = float(signal.get("sl", 0))
    risk_dist = abs(entry - sl)

    tick_value = float(symbol_spec.get("trade_tick_value") or 0)
    tick_size = float(symbol_spec.get("trade_tick_size") or symbol_spec.get("point") or 0)
    vmin = float(symbol_spec.get("volume_min") or 0.01)

    if risk_dist <= 0:
        ideal = default_lot
    else:
        risk_money = float(equity) * (risk_pct / 100.0)
        cap_usd = effective_risk_cap(config, balance, symbol=symbol)
        if cap_usd is not None:
            # Cap also scales by risk_parity share so a 0.25-share stream
            # can't blow through the per-trade USD cap at full size.
            scaled_cap = cap_usd * float(rp_share) if rp_active else cap_usd
            risk_money = min(risk_money, scaled_cap)
        if tick_value > 0 and tick_size > 0:
            ticks = risk_dist / tick_size
            ideal = risk_money / (ticks * tick_value)
        else:
            ideal = calc_risk_based_size(equity, risk_pct, entry, sl, max_size=max_lot)

    ideal = min(ideal, max_lot_for_symbol(config, symbol, max_lot))
    if ideal < vmin:
        ideal = min(max(default_lot, vmin), max_lot)

    # 2026-07-22 — symbol canary A/B: amplify the IDEAL size BEFORE all
    # downstream gates so exposure caps, USD risk caps, and pyramid scaling
    # see the boosted size and naturally shrink it back if needed. This
    # avoids the failure mode where the canary multiplier at the END of
    # the pipeline could push loss_usd past the risk cap and outright
    # reject the trade. Observe-only by design (state/canary_state.json
    # is the only persistent surface; no config mutation).
    try:
        from core.canary import lot_multiplier_for as _canary_mult
        _canary_boost = float(_canary_mult(symbol, config=config) or 1.0)
        _canary_boost = _canary_boost if _canary_boost > 1.0 else 1.0
    except Exception:
        _canary_boost = 1.0
    if _canary_boost > 1.0:
        ideal = ideal * _canary_boost
        ideal = max(vmin, min(ideal, max_lot))
        details["canary_multiplier"] = round(_canary_boost, 3)

    # Scalable lot sizing: scale the ideal lot by signal confidence and current
    # drawdown. High confidence + low drawdown -> larger lot (up to max_lot);
    # low confidence or deep drawdown -> de-risk toward min lot. Bounded by
    # max_lot, exposure caps, and the per-trade USD risk cap below.
    scaling = (config.get("signals") or {}).get("lot_scaling") or {}
    if scaling.get("enabled", False):
        try:
            from core.utils import read_json_state as _rjs
            _rs = _rjs("risk_state.json", default={}) or {}
            _dd = float(_rs.get("drawdown") or 0.0)
        except Exception:
            _dd = 0.0
        _conf = float(signal.get("confidence") or 0)
        _conf_ref = float(scaling.get("confidence_ref", 60))
        _conf_factor = max(0.5, min(2.5, _conf / _conf_ref)) if _conf_ref > 0 else 1.0
        _max_dd = float((config.get("risk") or {}).get("max_drawdown_pct", 15) or 15)
        _dd_factor = max(0.3, min(1.0, 1 - _dd / _max_dd)) if _max_dd > 0 else 1.0
        ideal = ideal * _conf_factor * _dd_factor
        ideal = max(vmin, min(ideal, max_lot))
        _ls_info = {
            "confidence": _conf, "confidence_factor": round(_conf_factor, 3),
            "drawdown_pct": round(_dd, 2), "drawdown_factor": round(_dd_factor, 3),
            "scaled_ideal": round(ideal, 4),
        }
    else:
        _ls_info = None

    capped, allowed = cap_size_to_exposure_limits(
        ideal,
        entry,
        symbol,
        positions,
        config,
        min_size=vmin,
        sl=sl,
        symbol_spec=symbol_spec,
    )
    details: dict[str, Any] = {
        "ideal_size": round(ideal, 4),
        "capped_size": capped,
        "exposure_allowed": allowed,
        "volume_min": vmin,
        "kelly": kelly,
        "risk_percent": risk_pct,
    }
    if _ls_info:
        details["lot_scaling"] = _ls_info

    if not allowed or capped <= 0:
        details["reject_reason"] = "exposure_cap_below_min_lot"
        return 0.0, details

    vol = capped
    if vol < vmin:
        risk_cfg = config.get("risk", {})
        max_symbol = float(risk_cfg.get("max_symbol_exposure_usd", equity))
        max_total = float(risk_cfg.get("max_total_exposure_usd", equity))
        vmin_exposure = position_exposure_usd(entry, vmin, sl=sl, symbol_spec=symbol_spec)
        if vmin_exposure > max_symbol + 0.01 or vmin_exposure > max_total + 0.01:
            details["reject_reason"] = "min_lot_notional_exceeds_exposure"
            return 0.0, details
        vol = vmin

    layer = int(signal.get("pyramid_layer", pyramid_layer_index(signal, positions)))
    vol = scale_lot_for_layer(config, symbol, vol, layer)

    # 2026-07-22 — symbol canary A/B: when today's canary pick matches
    # `symbol`, apply `canary_state.lot_multiplier` (default 1.5x) AT
    # THE IDEAL-SIZE LAYER (before exposure + USD risk-cap gates) so the
    # downstream gates see the amplified size and either keep or cap it
    # down without outright rejection. Observe-only by design — the
    # canary state file is the only persistent state.
    try:
        from core.canary import lot_multiplier_for as _canary_mult
        _canary = float(_canary_mult(symbol, config=config) or 1.0)
        _canary = _canary if _canary > 1.0 else 1.0
    except Exception:
        _canary = 1.0
    if _canary > 1.0:
        details["canary_multiplier"] = round(_canary, 3)

    vol = normalize_volume(vol, symbol_spec)

    cap_usd = effective_risk_cap(config, balance, symbol=symbol)
    if cap_usd is not None and risk_dist > 0 and vol > 0:
        scaled_cap = cap_usd * float(rp_share) if rp_active else cap_usd
        loss_usd = estimate_stop_loss_usd(
            risk_dist=risk_dist,
            volume=vol,
            tick_value=tick_value,
            tick_size=tick_size,
        )
        details["stop_loss_usd"] = round(loss_usd, 2)
        details["risk_cap_usd"] = round(scaled_cap, 2)
        if loss_usd > scaled_cap + 0.05:
            # 2026-07-22 — canary-friendly shrink-to-fit: if the canary
            # boost pushed loss_usd over the cap, scale vol DOWN
            # proportionally (with a 5% safety pad) rather than outright
            # rejecting the trade. Preserves the A/B experiment flow.
            _cbo = float(details.get("canary_multiplier", 1.0) or 1.0)
            if _cbo > 1.0 and vol > 0 and loss_usd > 0:
                _target = vol * (scaled_cap * 0.95) / loss_usd
                _target_n = normalize_volume(_target, symbol_spec)
                if _target_n >= vmin:
                    loss_usd = estimate_stop_loss_usd(
                        risk_dist=risk_dist, volume=_target_n,
                        tick_value=tick_value, tick_size=tick_size,
                    )
                    if loss_usd <= scaled_cap + 0.05:
                        vol = _target_n
                        details["canary_capped_to_fit"] = True
                        details["stop_loss_usd"] = round(loss_usd, 2)
            if loss_usd > scaled_cap + 0.05:
                # 2026-07-22 — clearer reject reason distinguishing
                # normal cap-exceeded from canary-boost-induced exceedance.
                if details.get("canary_multiplier", 1.0) > 1.0:
                    details["reject_reason"] = "canary_boost_cannot_fit_under_risk_cap"
                    details["canary_overshoot_usd"] = round(loss_usd - scaled_cap, 4)
                else:
                    details["reject_reason"] = "min_lot_stop_risk_exceeds_cap"
                return 0.0, details

    details["executable_volume"] = vol
    return vol, details