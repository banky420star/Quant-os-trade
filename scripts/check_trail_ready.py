"""Verify per-symbol BE/trailing config is complete for arena symbols."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.position_manager import _be_cfg, _symbol_overrides, _trail_cfg, _trail_distance_price
from core.strategy_arena import arena_settings
from core.utils import load_config

REQUIRED_KEYS = ("activation_profit_usd",)
ACTIVATION_FALLBACK = ("activation_points", "activation_atr_mult")
TRAIL_DISTANCE = ("trail_points", "trail_points_atr_mult", "trail_atr_mult", "trail_use_atr")


def _has_activation(trail_sym: dict, trail_parent: dict) -> bool:
    if trail_sym.get("activation_profit_usd") or trail_parent.get("activation_profit_usd"):
        return True
    return any(trail_sym.get(k) or trail_parent.get(k) for k in ACTIVATION_FALLBACK)


def _has_trail_distance(trail_sym: dict, trail_parent: dict) -> bool:
    return any(trail_sym.get(k) is not None or trail_parent.get(k) is not None for k in TRAIL_DISTANCE)


def check() -> int:
    config = load_config()
    arena = arena_settings(config)
    symbols = arena["symbols"] or config.get("mt5", {}).get("symbols", [])
    be_parent = _be_cfg(config)
    trail_parent = _trail_cfg(config)
    trailing_on = bool(trail_parent.get("enabled", True))

    print(f"trailing.enabled={trailing_on}")
    print(f"symbols ({len(symbols)}): {', '.join(symbols)}")
    print()

    ok = True
    for sym in symbols:
        be_sym = _symbol_overrides(be_parent, sym)
        trail_sym = _symbol_overrides(trail_parent, sym)
        issues: list[str] = []
        if not _has_activation(trail_sym, trail_parent):
            issues.append("missing trail activation (usd/points/atr)")
        if not _has_trail_distance(trail_sym, trail_parent):
            issues.append("missing trail distance (points/atr_mult)")
        be_usd = be_sym.get("trigger_profit_usd", be_parent.get("trigger_profit_usd"))
        act_usd = trail_sym.get("activation_profit_usd", trail_parent.get("activation_profit_usd"))
        if be_usd and act_usd and float(be_usd) > float(act_usd):
            issues.append(f"BE ${be_usd} > trail ${act_usd}")

        # Smoke-test trail distance with typical Exness point sizes
        point_guess = {
            "XAUUSDm": 0.01,
            "USOILm": 0.01,
            "BTCUSDm": 0.01,
            "NAS100m": 0.01,
            "US500m": 0.01,
            "JP225m": 1.0,
        }
        atr_guess = {
            "XAUUSDm": 3.5,
            "USOILm": 0.35,
            "BTCUSDm": 150.0,
            "NAS100m": 60.0,
            "US500m": 16.0,
            "JP225m": 120.0,
        }
        try:
            dist = _trail_distance_price(
                trail_sym, trail_parent,
                atr=atr_guess.get(sym, 1.0),
                point=point_guess.get(sym, 0.0001),
            )
            dist_s = f"{dist:.4f}"
        except Exception as exc:
            dist_s = f"ERR: {exc}"
            issues.append("trail distance calc failed")

        status = "READY" if not issues else "FIX"
        if issues:
            ok = False
        act_pts = trail_sym.get("activation_points", "—")
        trail_mode = (
            "atr_pts" if trail_sym.get("trail_points_atr_mult")
            else "fixed_pts" if trail_sym.get("trail_points")
            else "parent"
        )
        print(
            f"  {sym:<10} {status:<5}  "
            f"trail@${act_usd} pts={act_pts}  "
            f"BE@${be_sym.get('trigger_profit_usd', be_parent.get('trigger_profit_usd'))}  "
            f"dist={dist_s}  mode={trail_mode}"
        )
        for issue in issues:
            print(f"           ! {issue}")

    print()
    if ok:
        print("All arena symbols trail-ready.")
        return 0
    print("Some symbols need config fixes.")
    return 1


if __name__ == "__main__":
    raise SystemExit(check())