"""Canonical symbol list for full growth / arena campaigns."""

from __future__ import annotations

# All markets configured in sl_tp + symbol_manager (Exness demo).
ALL_CAMPAIGN_SYMBOLS: tuple[str, ...] = (
    "XAUUSDm",
    "USOILm",
    "BTCUSDm",
    "EURUSDm",
    "GBPUSDm",
    "USDJPYm",
    "USDCHFm",
    "AUDUSDm",
    "US500m",
    "US30m",
    "NAS100m",
    "UK100m",
    "FR40m",
    "JP225m",
)


def apply_all_symbols(config: dict) -> dict:
    """Copy full symbol list into mt5, practice, and arena sections."""
    syms = list(ALL_CAMPAIGN_SYMBOLS)
    config.setdefault("mt5", {})["symbols"] = syms
    config.setdefault("practice", {})["symbols"] = syms
    if config.get("strategy_arena", {}).get("enabled"):
        config.setdefault("strategy_arena", {})["symbols"] = syms
    return config