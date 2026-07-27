"""Startup profile guard — refuse to boot on the dangerous live+growth combo.

USER-AUTHORIZED 2026-07-01 ("collapse the mode switches so live vs growth
cannot silently drift"). On 2026-06-30 the real account was wiped to $0.00 by
a non-latching-kill churn loop that started because the real account was
running the aggressive growth profile with live trading enabled. The existing
``core.account_mode.validate_runtime_profile`` already refuses real+growth;
this guard complements it with:

  * an explicit top-level ``mode: growth|live|practice`` selector (the single
    canonical label for which profile is active), and
  * a pointed refusal for the EXACT wipe combination (real account + growth
    profile + live trading enabled), with a message naming the 2026-06-30
    incident so the failure is unmissable in the log.

Real money + aggressive growth must NEVER silently co-boot. Demo may run any
profile (paper). Called from start.py before any loop starts.
"""

from __future__ import annotations

import logging
from typing import Any

from core.account_mode import performance_gates_active
from core.blue_guardian import blue_guardian_enabled
from core.daily_growth import growth_plan_enabled

_VALID_MODES = {"growth", "live", "practice"}


def active_profile(config: dict[str, Any]) -> str:
    """Canonical active profile label derived from the scattered knobs."""
    if blue_guardian_enabled(config) and str(config.get("mt5", {}).get("account_mode", "")).lower() == "real":
        return "blue_guardian"
    if growth_plan_enabled(config):
        return "growth"
    if performance_gates_active(config):
        return "live"
    return "practice"


def assert_profile(config: dict[str, Any]) -> None:
    """Refuse startup when the account and active profile disagree.

    Raises RuntimeError on a dangerous/invalid combination so start.py aborts
    before any trading loop runs. Safe combinations return silently.
    """
    logger = logging.getLogger("profile_guard")
    account_mode = str(config.get("mt5", {}).get("account_mode", "demo")).lower()
    mode_field = config.get("mode")
    growth_active = growth_plan_enabled(config)
    perf_active = performance_gates_active(config)
    live_trading = bool(config.get("execution", {}).get("live_trading_enabled", False))
    derived = active_profile(config)

    # Validate the explicit selector if the operator set one.
    if mode_field is not None:
        mode_field = str(mode_field).lower()
        if mode_field not in _VALID_MODES:
            raise RuntimeError(
                f"config.mode '{mode_field}' is not a valid profile "
                f"(expected one of {sorted(_VALID_MODES)})"
            )
        if mode_field != derived:
            logger.warning(
                "config.mode=%s disagrees with derived active profile=%s "
                "(growth_enabled=%s, perf_active=%s); deriving from knobs.",
                mode_field, derived, growth_active, perf_active,
            )

    bg_eval = blue_guardian_enabled(config) and account_mode == "real"

    if account_mode == "real":
        # The 2026-06-30 wipe: real money + aggressive growth + live trading.
        if growth_active and live_trading and not bg_eval:
            raise RuntimeError(
                "REFUSING TO START: real account + aggressive growth profile + "
                "live_trading_enabled=true is the exact combination that wiped "
                "account 295959027 to $0.00 on 2026-06-30. Set mt5.account_mode: "
                "demo, or set execution.live_trading_enabled: false, or disable "
                "practice.growth before booting a real account."
            )
        # Real account must run the conservative live/performance profile, not
        # the growth (practice) profile — even with live trading off, growth on
        # real is a misconfiguration (validate_runtime_profile also enforces
        # this; we re-state it with the mode label for clarity).
        if growth_active and not bg_eval:
            raise RuntimeError(
                "REFUSING TO START: real account cannot run the growth (practice) "
                "profile. Set config.mode: live and disable practice.growth, or "
                "switch to mt5.account_mode: demo."
            )
        if mode_field is not None and mode_field not in ("live", "blue_guardian") and not bg_eval:
            raise RuntimeError(
                f"REFUSING TO START: real account requires config.mode: live "
                f"(got '{mode_field}'). A real account must run the conservative "
                f"performance profile or blue_guardian eval."
            )

    # Demo may run any profile (paper). Just log the derived label.
    logger.info(
        "Profile guard OK: account=%s derived_profile=%s mode_field=%s "
        "growth=%s perf=%s live_trading=%s",
        account_mode, derived, mode_field, growth_active, perf_active, live_trading,
    )