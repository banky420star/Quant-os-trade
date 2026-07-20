"""Adaptive gate throttle — the bot tightens its entry gates when it is losing
and relaxes them when it is winning, and auto-pauses any symbol on a losing
streak. This is the ""adapt to losses and change conditions"" layer: instead of
static thresholds, the evaluation gates move with recent realized performance.

State is written to state/adaptive_gates.json each evaluation cycle so the
dashboard/TUI can surface the current tier (normal / cautious / defensive).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from core.utils import read_json_state, utc_now_iso, write_json_state

STATE_FILE = "adaptive_gates.json"


def _adapt_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return (config.get("adaptation") or {}).get("adaptive_gates") or {}


def adaptive_gates_enabled(config: dict[str, Any]) -> bool:
    return bool(_adapt_cfg(config).get("enabled", False))


def _is_loss(t: dict[str, Any]) -> bool:
    if t.get("result") == "loss":
        return True
    pnl = t.get("pnl")
    if pnl is not None:
        try:
            return float(pnl) < 0
        except (TypeError, ValueError):
            return False
    return False


def _is_win(t: dict[str, Any]) -> bool:
    if t.get("result") == "win":
        return True
    pnl = t.get("pnl")
    if pnl is not None:
        try:
            return float(pnl) > 0
        except (TypeError, ValueError):
            return False
    return False


def _recent_closed_trades(limit: int) -> list[dict[str, Any]]:
    """Most-recent-first closed trades from trade_log (falls back to paper_trades)."""
    tl = read_json_state("trade_log.json", default={}) or {}
    trades = list(tl.get("trades") or [])
    if not trades:
        pt = read_json_state("paper_trades.json", default={"trades": []}) or {}
        trades = list(pt.get("trades") or [])
    trades = sorted(trades, key=lambda t: t.get("closed_at") or t.get("filled_at") or "", reverse=True)
    return trades[:limit]


def _trailing_consecutive(rows: list[dict[str, Any]]) -> int:
    """Consecutive losses counting from the most recent close."""
    streak = 0
    for t in rows:
        if _is_loss(t):
            streak += 1
        elif _is_win(t):
            break
        else:
            break
    return streak


def compute_adaptive_gates(config: dict[str, Any]) -> dict[str, Any]:
    """Return runtime gate overrides derived from recent closed trades."""
    cfg = _adapt_cfg(config)
    base_eval = dict(config.get("evaluation") or {})
    base_min_score = float(base_eval.get("min_policy_score") or 35)
    base_min_conf = float((config.get("signals") or {}).get("min_confidence") or 50)
    base_blocklist = list(base_eval.get("symbol_blocklist") or [])

    baseline = {
        "enabled": adaptive_gates_enabled(config),
        "tier": "off",
        "min_policy_score": base_min_score,
        "min_confidence": base_min_conf,
        "blocked_symbols": base_blocklist,
        "lookback_n": 0,
        "recent_win_rate_pct": 50.0,
        "recent_net_pnl": 0.0,
        "consecutive_losses": 0,
        "reason": "disabled",
        "timestamp": utc_now_iso(),
    }

    if not baseline["enabled"]:
        write_json_state(STATE_FILE, baseline)
        return baseline

    lookback = int(cfg.get("lookback", 15))
    min_sample = int(cfg.get("min_sample", 5))
    trades = _recent_closed_trades(lookback)
    n = len(trades)
    wins = sum(1 for t in trades if _is_win(t))
    win_rate = 100.0 * wins / n if n else 50.0
    net = 0.0
    for t in trades:
        try:
            net += float(t.get("pnl") or 0)
        except (TypeError, ValueError):
            pass
    consec = _trailing_consecutive(trades)

    # Per-symbol auto-pause: a symbol that lost its last `auto_block_streak`
    # trades is added to the runtime blocklist until it posts a win again.
    auto_streak = int(cfg.get("auto_block_streak", 3))
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        sym = t.get("symbol")
        if sym:
            by_sym[sym].append(t)
    blocked = set(base_blocklist)
    for sym, rows in by_sym.items():
        rows_sorted = sorted(rows, key=lambda t: t.get("closed_at") or "", reverse=True)
        if _trailing_consecutive(rows_sorted) >= auto_streak:
            blocked.add(sym)

    cautious_wr = float(cfg.get("cautious_win_rate", 40))
    defensive_wr = float(cfg.get("defensive_win_rate", 28))
    defensive_streak = int(cfg.get("defensive_consecutive", 4))
    tight_cautious_score = float(cfg.get("tighten_cautious_score", 10))
    tight_defensive_score = float(cfg.get("tighten_defensive_score", 20))
    tight_cautious_conf = float(cfg.get("tighten_cautious_conf", 5))
    tight_defensive_conf = float(cfg.get("tighten_defensive_conf", 10))

    if n >= min_sample and (win_rate < defensive_wr or consec >= defensive_streak):
        tier = "defensive"
        min_score = base_min_score + tight_defensive_score
        min_conf = base_min_conf + tight_defensive_conf
        reason = f"defensive win={win_rate:.0f}% consec={consec} net={net:.2f}"
    elif n >= min_sample and win_rate < cautious_wr:
        tier = "cautious"
        min_score = base_min_score + tight_cautious_score
        min_conf = base_min_conf + tight_cautious_conf
        reason = f"cautious win={win_rate:.0f}% net={net:.2f}"
    else:
        tier = "normal"
        min_score = base_min_score
        min_conf = base_min_conf
        reason = f"normal win={win_rate:.0f}% net={net:.2f}"

    out = {
        "enabled": True,
        "tier": tier,
        "min_policy_score": min_score,
        "min_confidence": min_conf,
        "blocked_symbols": sorted(blocked),
        "lookback_n": n,
        "recent_win_rate_pct": round(win_rate, 1),
        "recent_net_pnl": round(net, 2),
        "consecutive_losses": consec,
        "reason": reason,
        "timestamp": utc_now_iso(),
    }
    write_json_state(STATE_FILE, out)
    return out


def read_adaptive_gates() -> dict[str, Any]:
    return read_json_state(STATE_FILE, default={}) or {}
