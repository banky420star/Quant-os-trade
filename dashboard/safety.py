"""Phase 0 dashboard safety helpers. Imported by server.py."""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone as _tz
from typing import Any


def build_safety_header(
    account: dict,
    config: dict | None = None,
    live_portfolio: dict | None = None,
    health: dict | None = None,
    kill_switch: dict | None = None,
    *,
    active_profile_name_fn=None,
) -> dict[str, Any]:
    """Phase 0: permanent safety matrix for every dashboard page."""
    cfg = config or {}
    exec_cfg = cfg.get("execution") or {}
    profile = (cfg.get("profile") or {}).get("name")
    if not profile and active_profile_name_fn:
        profile = active_profile_name_fn()
    profile = profile or "?"

    account_mode = exec_cfg.get("mode", "paper")
    explicit_opt_in = bool(exec_cfg.get("explicit_opt_in_danger_zone", False))
    fast_cfg = cfg.get("fast_mode") or {}
    learning_cfg = cfg.get("learning") or {}
    adaptation_cfg = cfg.get("adaptation") or {}

    lp = live_portfolio or {}
    positions = lp.get("open_positions", [])
    # Phase 0: compute actual stop-risk, not floating P&amp;L
    stop_risk = 0.0
    for p in positions:
        sl_price = p.get("sl") or p.get("stop_loss") or 0.0
        entry_price = p.get("entry") or p.get("open_price") or 0.0
        volume = float(p.get("volume", 0) or 0)
        if sl_price and entry_price and volume:
            stop_risk += abs(sl_price - entry_price) * volume
    open_risk = round(stop_risk, 2) if stop_risk else round(sum(abs(float(p.get("profit", 0))) for p in positions), 2)

    now_ts = _time.time()
    data_updated = lp.get("updated_at") or ""
    data_age_s = -1.0
    if data_updated:
        try:
            dt = datetime.fromisoformat(data_updated.replace("Z", "+00:00"))
            data_age_s = round(now_ts - dt.timestamp(), 1)
        except Exception:
            pass

    if data_age_s < 0:
        freshness = "unknown"
    elif data_age_s > 30:
        freshness = "stale"
    elif data_age_s > 10:
        freshness = "aging"
    elif data_age_s > 2:
        freshness = "warm"
    else:
        freshness = "fresh"

    health_status = "unknown"
    if health:
        hs = health.get("status", "")
        if hs == "critical":
            health_status = "critical"
        elif hs == "degraded":
            health_status = "degraded"
        elif hs == "ok":
            health_status = "ok"

    kill_on = bool((kill_switch or {}).get("kill_switch", False))
    fast_live = bool(fast_cfg.get("live_enabled", False))
    fast_enabled = bool(fast_cfg.get("enabled", False))
    adaptation_on = bool(adaptation_cfg.get("enabled", False))
    learning_mode = learning_cfg.get("mode", "observe_only")

    return {
        "account_mode": account_mode,
        "account_login": lp.get("account_login"),
        "account_server": lp.get("account_server"),
        "profile": profile,
        "execution_allowed": explicit_opt_in,
        "fast_mode": "LIVE" if fast_live else ("observe" if fast_enabled else "OFF"),
        "adaptation": "ON" if adaptation_on else "OFF",
        "learning": learning_mode,
        "kill_switch": kill_on,
        "health": health_status,
        "open_positions": len(positions),
        "open_risk": open_risk,
        "data_source": lp.get("source"),
        "data_freshness": freshness,
        "data_age_seconds": data_age_s,
    }
