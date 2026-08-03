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

from core.trade_history import read_closed_trades
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


def _recent_closed_trades(config: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Most-recent-first rows from the active execution-mode ledger."""
    # Pass this module's reader explicitly so isolated tests and callers that
    # provide a state backend remain effective; production still uses JSON.
    return read_closed_trades(config, limit=limit, reader=read_json_state)


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
    trades = _recent_closed_trades(config, lookback)
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
    # Net-PnL gates (2026-07-15): live book had 67% WR with net=-20 — WR-only
    # gates stayed "normal" while the account bled. Expectancy is the real signal.
    cautious_net = float(cfg.get("cautious_net_pnl", -5.0))
    defensive_net = float(cfg.get("defensive_net_pnl", -15.0))
    # Auto-block symbols with deep net loss in the lookback (not just streak).
    symbol_net_block = float(cfg.get("symbol_net_block_pnl", -8.0))
    symbol_net_min_n = int(cfg.get("symbol_net_block_min_n", 5))
    for sym, rows in by_sym.items():
        if len(rows) < symbol_net_min_n:
            continue
        sym_net = 0.0
        for t in rows:
            try:
                sym_net += float(t.get("pnl") or 0)
            except (TypeError, ValueError):
                pass
        if sym_net <= symbol_net_block:
            blocked.add(sym)

    # Gold never auto-paused — re-strip AFTER streak + net-block (review bug).
    try:
        from core.gold_policy import is_gold_symbol, gold_never_rejectable

        if gold_never_rejectable(config):
            blocked = {s for s in blocked if not is_gold_symbol(s)}
    except Exception:
        pass

    tight_cautious_score = float(cfg.get("tighten_cautious_score", 10))
    tight_defensive_score = float(cfg.get("tighten_defensive_score", 20))
    tight_cautious_conf = float(cfg.get("tighten_cautious_conf", 5))
    tight_defensive_conf = float(cfg.get("tighten_defensive_conf", 10))

    # Payoff ratio (avg_win / |avg_loss|) — high WR with tiny wins is still toxic.
    win_pnls = []
    loss_pnls = []
    for t in trades:
        try:
            p = float(t.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
        if p > 0:
            win_pnls.append(p)
        elif p < 0:
            loss_pnls.append(p)
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    payoff = abs(avg_win / avg_loss) if avg_loss != 0 else None
    weak_payoff = float(cfg.get("weak_payoff_ratio", 0.6))
    defensive_payoff = float(cfg.get("defensive_payoff_ratio", 0.4))

    if n >= min_sample and (
        win_rate < defensive_wr
        or consec >= defensive_streak
        or net <= defensive_net
        or (payoff is not None and payoff < defensive_payoff and win_rate >= 50)
    ):
        tier = "defensive"
        min_score = base_min_score + tight_defensive_score
        min_conf = base_min_conf + tight_defensive_conf
        reason = (
            f"defensive win={win_rate:.0f}% consec={consec} net={net:.2f}"
            f" payoff={payoff if payoff is not None else 'n/a'}"
        )
    elif n >= min_sample and (
        win_rate < cautious_wr
        or net <= cautious_net
        or (payoff is not None and payoff < weak_payoff and win_rate >= 55)
    ):
        tier = "cautious"
        min_score = base_min_score + tight_cautious_score
        min_conf = base_min_conf + tight_cautious_conf
        reason = (
            f"cautious win={win_rate:.0f}% net={net:.2f}"
            f" payoff={payoff if payoff is not None else 'n/a'}"
        )
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
        "recent_payoff_ratio": round(payoff, 4) if payoff is not None else None,
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "consecutive_losses": consec,
        "reason": reason,
        "timestamp": utc_now_iso(),
    }
    write_json_state(STATE_FILE, out)
    return out


def read_adaptive_gates() -> dict[str, Any]:
    return read_json_state(STATE_FILE, default={}) or {}
