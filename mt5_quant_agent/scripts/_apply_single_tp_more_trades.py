"""Apply single-TP + more-trades config flip to mt5_quant_agent/config.yaml.

Single TP:
- tp2_rr = tp1_rr per symbol (effectively one target)
- exits.partial_tp.enabled = false
- exits.runner.extend_tp_to_tp2 = false

More trades (frequency dial-up, moderate):
- signals.min_confidence: 40 -> 35
- practice.symbols.min_confidence: 58 -> 40
- practice.symbols.min_trade_score: 20 -> 18
- practice.growth.max_session_trades_per_symbol: 36 -> 60
- practice.growth.min_confidence: 45 -> 38
- practice.growth.min_trade_score: 22 -> 18
- fast_mode.symbols: extend to cover ALL trading symbols so 1s scanner hits them
- signals.min_risk_reward: 1.2 -> 1.05 (lower friction on the R:R gate)
"""
from __future__ import annotations

import sys
import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config.yaml"


def _load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _dump(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _set_per_symbol_tp2_eq_tp1(cfg: dict) -> list[str]:
    """For every per_symbol block under trading.strategy_entries.sl_tp, set tp2_rr = tp1_rr."""
    sl_tp = cfg.get("trading", {}).get("strategy_entries", {}).get("sl_tp", {})
    per_sym = sl_tp.get("per_symbol", {}) or {}
    syms = []
    for sym, block in per_sym.items():
        if not isinstance(block, dict):
            continue
        tp1 = block.get("tp1_rr")
        if tp1 is None:
            continue
        old = block.get("tp2_rr")
        if old != tp1:
            block["tp2_rr"] = tp1
            syms.append(f"{sym}: tp2_rr {old} -> {tp1}")
    # also flatten the global default
    g_tp1 = sl_tp.get("tp1_rr")
    if g_tp1 is not None and sl_tp.get("tp2_rr") != g_tp1:
        old = sl_tp["tp2_rr"]
        sl_tp["tp2_rr"] = g_tp1
        syms.append(f"GLOBAL tp2_rr {old} -> {g_tp1}")
    return syms


def main() -> int:
    cfg = _load(CFG)
    changes: list[str] = []

    # ---- Single TP ----
    # 1) flatten per_symbol tp2_rr = tp1_rr + flatten the global default
    changes += _set_per_symbol_tp2_eq_tp1(cfg)

    # 2) disable partial TP at the exits level
    exits = cfg.setdefault("trading", {}).setdefault("exits", {})
    pt = exits.setdefault("partial_tp", {})
    if pt.get("enabled") is not False:
        pt["enabled"] = False
        changes.append("trading.exits.partial_tp.enabled -> False")

    # 3) disable runner.extend_tp_to_tp2 so the bot does not chase a runner
    runner = exits.setdefault("runner", {})
    if runner.get("extend_tp_to_tp2") is not False:
        runner["extend_tp_to_tp2"] = False
        changes.append("trading.exits.runner.extend_tp_to_tp2 -> False")

    # 4) same for practice.micro if present (the bot may mirror via practice path)
    micro_exits = (
        cfg.get("practice", {}).get("micro", {}).get("exits", {})
    )
    if micro_exits:
        mp = micro_exits.setdefault("partial_tp", {})
        if mp.get("enabled") is not False:
            mp["enabled"] = False
            changes.append("practice.micro.exits.partial_tp.enabled -> False")
        mr = micro_exits.setdefault("runner", {})
        if mr.get("extend_tp_to_tp2") is not False:
            mr["extend_tp_to_tp2"] = False
            changes.append("practice.micro.exits.runner.extend_tp_to_tp2 -> False")

    # ---- More trades ----
    # 5) lower signals.min_confidence (top-level filter)
    sig = cfg.setdefault("signals", {})
    if sig.get("min_confidence", 0) > 35:
        old = sig["min_confidence"]
        sig["min_confidence"] = 35
        changes.append(f"signals.min_confidence {old} -> 35")

    # 6) lower signals.min_risk_reward to ease the R:R gate
    if sig.get("min_risk_reward", 0) > 1.05:
        old = sig["min_risk_reward"]
        sig["min_risk_reward"] = 1.05
        changes.append(f"signals.min_risk_reward {old} -> 1.05")

    # 7) practice.symbols (the real feeder for $30 growth on demo)
    syms = cfg.setdefault("practice", {}).setdefault("symbols_list_min_conf_holder", {})  # nose-bleed guard
    practice_symbols = cfg.setdefault("practice", {})
    if "symbols" in practice_symbols and isinstance(practice_symbols["symbols"], dict):
        ps = practice_symbols["symbols"]
        if ps.get("min_confidence", 0) > 40:
            old = ps["min_confidence"]
            ps["min_confidence"] = 40
            changes.append(f"practice.symbols.min_confidence {old} -> 40")
        if ps.get("min_trade_score", 0) > 18:
            old = ps["min_trade_score"]
            ps["min_trade_score"] = 18
            changes.append(f"practice.symbols.min_trade_score {old} -> 18")

    # 8) practice.growth profile (frequency knob)
    growth = cfg.setdefault("practice", {}).setdefault("growth", {})
    if growth.get("max_session_trades_per_symbol", 0) < 60:
        old = growth.get("max_session_trades_per_symbol")
        growth["max_session_trades_per_symbol"] = 60
        changes.append(f"practice.growth.max_session_trades_per_symbol {old} -> 60")
    if growth.get("min_confidence", 0) > 38:
        old = growth["min_confidence"]
        growth["min_confidence"] = 38
        changes.append(f"practice.growth.min_confidence {old} -> 38")
    if growth.get("min_trade_score", 0) > 18:
        old = growth["min_trade_score"]
        growth["min_trade_score"] = 18
        changes.append(f"practice.growth.min_trade_score {old} -> 18")
    if growth.get("min_risk_reward", 0) > 0.9:
        old = growth["min_risk_reward"]
        growth["min_risk_reward"] = 0.9
        changes.append(f"practice.growth.min_risk_reward {old} -> 0.9")

    # 9) extend fast_mode.symbols to cover ALL mt5.symbols so the 1s scanner hits them
    fm = cfg.setdefault("fast_mode", {})
    all_syms = list(cfg.get("mt5", {}).get("symbols", []) or [])
    fm_set = set(fm.get("symbols", []) or [])
    added = [s for s in all_syms if s not in fm_set]
    if added:
        fm["symbols"] = list(fm.get("symbols", []) or []) + added
        changes.append(f"fast_mode.symbols added: {added}")

    _dump(CFG, cfg)
    print(f"Applied {len(changes)} changes:")
    for c in changes:
        print(f"  - {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
