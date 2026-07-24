"""Babysit metrics — observe live stack for profitability regressions.

Used by observe cycles and unit tests. Pure functions over trade book + config
so each babysit pass can assert pass/fail without dashboards.
"""

from __future__ import annotations

from typing import Any


def equity_from_account(account: dict[str, Any] | None) -> float | None:
    """Prefer equity, fall back to balance. None if unreadable."""
    if not isinstance(account, dict):
        return None
    for key in ("equity", "balance"):
        try:
            v = account.get(key)
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def trade_book_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Expectancy, payoff, WR, bleed-by-symbol from closed trades."""
    clean = [t for t in trades if not t.get("archive_polluted")]
    n = len(clean)
    if n == 0:
        return {
            "n": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "total_pnl": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "payoff_ratio": None,
            "expectancy_r": None,
            "avg_r": None,
            "r_coverage": 0,
            "entry_eq_exit_count": 0,
            "by_symbol": {},
            "by_setup": {},
        }

    wins = [t for t in clean if t.get("result") == "win" or float(t.get("pnl") or 0) > 0]
    losses = [t for t in clean if t.get("result") == "loss" or float(t.get("pnl") or 0) < 0]
    win_pnls = [float(t.get("pnl") or 0) for t in wins]
    loss_pnls = [float(t.get("pnl") or 0) for t in losses]
    total_pnl = sum(float(t.get("pnl") or 0) for t in clean)
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    payoff = abs(avg_win / avg_loss) if avg_loss != 0 else None

    rs = []
    for t in clean:
        if t.get("r_multiple") is not None:
            try:
                rs.append(float(t["r_multiple"]))
            except (TypeError, ValueError):
                pass
    avg_r = sum(rs) / len(rs) if rs else None
    entry_eq = sum(
        1
        for t in clean
        if t.get("entry") is not None
        and t.get("exit") is not None
        and abs(float(t["entry"]) - float(t["exit"])) < 1e-12
    )

    by_symbol: dict[str, dict[str, Any]] = {}
    by_setup: dict[str, dict[str, Any]] = {}
    for t in clean:
        sym = str(t.get("symbol") or "?")
        setup = str(t.get("setup") or t.get("setup_type") or "?")
        for bucket, key in ((by_symbol, sym), (by_setup, setup)):
            row = bucket.setdefault(key, {"n": 0, "pnl": 0.0, "wins": 0})
            row["n"] += 1
            row["pnl"] += float(t.get("pnl") or 0)
            if t.get("result") == "win" or float(t.get("pnl") or 0) > 0:
                row["wins"] += 1

    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / n, 2),
        "total_pnl": round(total_pnl, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "payoff_ratio": round(payoff, 4) if payoff is not None else None,
        "expectancy_r": round(avg_r, 4) if avg_r is not None else None,
        "avg_r": round(avg_r, 4) if avg_r is not None else None,
        "r_coverage": len(rs),
        "entry_eq_exit_count": entry_eq,
        "by_symbol": by_symbol,
        "by_setup": by_setup,
    }


def xau_be_points(config: dict[str, Any]) -> dict[str, Any]:
    """Loaded XAU break-even points (after micro merge should already be applied)."""
    be = (config.get("trading") or {}).get("break_even") or {}
    xau = (be.get("per_symbol") or {}).get("XAUUSDm") or {}
    return {
        "trigger_points": xau.get("trigger_points", be.get("trigger_points")),
        "lock_profit_points": xau.get("lock_profit_points", be.get("lock_profit_points")),
        "trigger_profit_usd": xau.get("trigger_profit_usd", be.get("trigger_profit_usd")),
        "lock_profit_usd": xau.get("lock_profit_usd", be.get("lock_profit_usd")),
    }


def detect_regressions(
    config: dict[str, Any],
    book: dict[str, Any],
    gates: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Return list of {id, severity, detail} for prior-class regressions."""
    issues: list[dict[str, str]] = []
    be = xau_be_points(config)
    try:
        usd = float(be.get("trigger_profit_usd") or 999)
    except (TypeError, ValueError):
        usd = 999.0
    try:
        pts = float(be.get("trigger_points") or 0)
    except (TypeError, ValueError):
        pts = 0.0

    # Tight USD BE while points missing or low = classic payoff killer
    if usd < 10 and pts < 1000:
        issues.append({
            "id": "tight_be_usd",
            "severity": "critical",
            "detail": f"XAU BE usd={usd} pts={pts} (want pts>=5000 or usd>=15)",
        })
    if pts and pts < 2000:
        issues.append({
            "id": "tight_be_points",
            "severity": "high",
            "detail": f"XAU trigger_points={pts} (want >=5000 for wide BE path)",
        })

    block = list((config.get("evaluation") or {}).get("symbol_blocklist") or [])
    for bleed in ("USOILm", "UK100m"):
        if bleed not in block:
            issues.append({
                "id": f"missing_block_{bleed}",
                "severity": "high",
                "detail": f"{bleed} not in symbol_blocklist",
            })
    setups = list((config.get("evaluation") or {}).get("setup_blocklist") or [])
    if "trend_continuation" not in setups:
        issues.append({
            "id": "missing_setup_block_trend",
            "severity": "high",
            "detail": "trend_continuation not in setup_blocklist",
        })

    trail = (config.get("trading") or {}).get("trailing") or {}
    if trail.get("enabled") is False:
        issues.append({
            "id": "trail_disabled",
            "severity": "medium",
            "detail": "trailing.enabled is false",
        })

    if book.get("n", 0) >= 20:
        wr = float(book.get("win_rate_pct") or 0)
        exp = book.get("expectancy_r")
        payoff = book.get("payoff_ratio")
        # Prefer active-universe metrics when present (blocked bleed no longer traded).
        active = book.get("active_universe") if isinstance(book.get("active_universe"), dict) else None
        if active and int(active.get("n") or 0) >= 15:
            wr_a = float(active.get("win_rate_pct") or 0)
            exp_a = active.get("expectancy_r")
            pay_a = active.get("payoff_ratio")
            if wr_a >= 55 and exp_a is not None and float(exp_a) < 0:
                issues.append({
                    "id": "high_wr_neg_expectancy",
                    "severity": "critical",
                    "detail": f"active WR={wr_a}% E[R]={exp_a} — payoff asymmetry",
                })
            if pay_a is not None and float(pay_a) < 0.6 and wr_a >= 50:
                issues.append({
                    "id": "weak_payoff",
                    "severity": "high",
                    "detail": f"active payoff_ratio={pay_a} (avg_win/|avg_loss|)",
                })
        else:
            if wr >= 55 and exp is not None and float(exp) < 0:
                issues.append({
                    "id": "high_wr_neg_expectancy",
                    "severity": "critical",
                    "detail": f"WR={wr}% E[R]={exp} — payoff asymmetry",
                })
            if payoff is not None and float(payoff) < 0.6 and wr >= 50:
                issues.append({
                    "id": "weak_payoff",
                    "severity": "high",
                    "detail": f"payoff_ratio={payoff} (avg_win/|avg_loss|)",
                })
        if int(book.get("entry_eq_exit_count") or 0) > max(5, int(book["n"] * 0.2)):
            issues.append({
                "id": "entry_eq_exit",
                "severity": "critical",
                "detail": f"entry==exit on {book['entry_eq_exit_count']}/{book['n']} trades",
            })
        if int(book.get("r_coverage") or 0) < max(10, int(book["n"] * 0.5)):
            issues.append({
                "id": "low_r_coverage",
                "severity": "high",
                "detail": f"R coverage {book.get('r_coverage')}/{book['n']}",
            })

    if gates and gates.get("enabled"):
        try:
            net = float(gates.get("recent_net_pnl") or 0)
        except (TypeError, ValueError):
            net = 0.0
        tier = str(gates.get("tier") or "")
        if net <= -5 and tier == "normal":
            issues.append({
                "id": "gates_ignore_net_pnl",
                "severity": "critical",
                "detail": f"tier=normal with recent_net_pnl={net}",
            })

    return issues


def supervisor_ok(supervisor: dict[str, Any] | None) -> bool:
    if not isinstance(supervisor, dict):
        return False
    status = str(supervisor.get("overall_status") or "").lower()
    if status in ("healthy", "ok", "running"):
        return True
    services = supervisor.get("services") or []
    names = {s.get("name"): s for s in services if isinstance(s, dict)}
    tp = names.get("trading_pipeline") or {}
    return str(tp.get("status") or "").lower() in ("running", "ok", "healthy")


def observe_snapshot(
    *,
    account: dict[str, Any],
    supervisor: dict[str, Any],
    trade_log: dict[str, Any],
    config: dict[str, Any],
    gates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full babysit observation package."""
    trades = list(trade_log.get("trades") or []) if isinstance(trade_log, dict) else []
    book = trade_book_summary(trades)
    # Prefer trade_log aggregates when present and richer
    if isinstance(trade_log, dict) and trade_log.get("total"):
        if book["n"] == 0 and int(trade_log.get("total") or 0) > 0:
            book["n"] = int(trade_log["total"])
            book["win_rate_pct"] = float(trade_log.get("win_rate_pct") or 0)
            book["total_pnl"] = float(trade_log.get("total_pnl") or 0)
            book["expectancy_r"] = trade_log.get("expectancy_R") or trade_log.get("avg_R")
            book["avg_r"] = trade_log.get("avg_R")
    # Active universe = currently allowed symbols/setups (excludes blocklisted bleed)
    block = set((config.get("evaluation") or {}).get("symbol_blocklist") or [])
    setup_block = set((config.get("evaluation") or {}).get("setup_blocklist") or [])
    micro_syms = list(((config.get("practice") or {}).get("micro") or {}).get("symbols") or [])
    allowed = set(micro_syms) if micro_syms else None
    active_trades = []
    for t in trades:
        sym = str(t.get("symbol") or "")
        setup = str(t.get("setup") or t.get("setup_type") or "")
        if sym in block or setup in setup_block:
            continue
        if allowed is not None and sym not in allowed:
            continue
        active_trades.append(t)
    if active_trades:
        book["active_universe"] = trade_book_summary(active_trades)
    equity = equity_from_account(account)
    issues = detect_regressions(config, book, gates)
    return {
        "equity": equity,
        "balance": account.get("balance") if isinstance(account, dict) else None,
        "equity_ge_500000": bool(equity is not None and equity >= 500_000),
        "supervisor_ok": supervisor_ok(supervisor),
        "supervisor_status": (supervisor or {}).get("overall_status"),
        "book": book,
        "xau_be": xau_be_points(config),
        "symbol_blocklist": list((config.get("evaluation") or {}).get("symbol_blocklist") or []),
        "setup_blocklist": list((config.get("evaluation") or {}).get("setup_blocklist") or []),
        "gates": {
            "tier": (gates or {}).get("tier"),
            "reason": (gates or {}).get("reason"),
            "recent_net_pnl": (gates or {}).get("recent_net_pnl"),
            "recent_win_rate_pct": (gates or {}).get("recent_win_rate_pct"),
        },
        "regressions": issues,
        "critical_regressions": [i for i in issues if i.get("severity") == "critical"],
        "healthy_ops": supervisor_ok(supervisor) and not any(
            i.get("severity") == "critical" and i.get("id") in (
                "tight_be_usd", "entry_eq_exit", "gates_ignore_net_pnl"
            )
            for i in issues
        ),
    }
