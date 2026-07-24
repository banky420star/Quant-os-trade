"""Gold (XAU) policy — never-rejectable path for gold candidates.

USER request 2026-07-15: gold trades must not be blocked by evaluation skips,
verifier quality gates, consensus, or adaptive symbol pauses. Kill-switch and
missing price levels remain hard safety (cannot place a trade with invalid SL).
"""

from __future__ import annotations

from typing import Any


def is_gold_symbol(symbol: str | None) -> bool:
    """True for XAU / GOLD CFD symbols (e.g. XAUUSDm, XAUUSD)."""
    s = str(symbol or "").upper().replace(".", "").replace("_", "")
    if not s:
        return False
    if s.startswith("XAU"):
        return True
    if "GOLD" in s:
        return True
    return False


def gold_never_rejectable(config: dict[str, Any] | None) -> bool:
    """Config flag; default False unless explicitly enabled (safer for live)."""
    cfg = config or {}
    signals = cfg.get("signals") if isinstance(cfg.get("signals"), dict) else {}
    if "gold_never_rejectable" in signals:
        return bool(signals.get("gold_never_rejectable"))
    if "gold_never_rejectable" in cfg:
        return bool(cfg.get("gold_never_rejectable"))
    return False


def gold_force_pass(symbol: str | None, config: dict[str, Any] | None) -> bool:
    """True when this symbol must bypass quality rejections."""
    return is_gold_symbol(symbol) and gold_never_rejectable(config)
