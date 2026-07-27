"""Equity curve — snapshot history and curve reconstruction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.utils import read_json_state, utc_now_iso, write_json_state

MAX_POINTS = 50_000

EQUITY_RANGE_SECONDS: dict[str, int | None] = {
    "1m": 60,
    "1h": 3600,
    "1d": 86400,
    "7d": 86400 * 7,
    "30d": 86400 * 30,
    "all": None,
}


def record_snapshot(
    equity: float,
    cash: float | None = None,
    *,
    source: str = "risk_loop",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append an equity snapshot (skip only if unchanged within 30s)."""
    history = read_json_state("equity_history.json", default={"points": []})
    points: list[dict[str, Any]] = list(history.get("points", []))
    now = utc_now_iso()
    cash_val = round(float(cash if cash is not None else equity), 2)
    equity_val = round(float(equity), 2)
    unrealized = round(equity_val - cash_val, 2)

    # Guard against transient bad MT5 reads (e.g. equity=100 on a $5k account)
    # that spike the equity-graph y-axis and flatten the real line. Skip
    # non-positive values and implausible >50% crashes from the last real
    # point — a real account does not halve in one 20s tick.
    if equity_val <= 0:
        return history
    prior = read_json_state("equity_history.json", default={"points": []})
    prior_pts = prior.get("points") or []
    if prior_pts:
        last_eq = float(prior_pts[-1].get("equity", 0) or 0)
        if last_eq > 0 and equity_val < last_eq * 0.5:
            return history

    point = {
        "ts": now,
        "equity": equity_val,
        "cash": cash_val,
        "balance": cash_val,
        "unrealized_pnl": unrealized,
        "source": source,
        **(extra or {}),
    }

    if points:
        last = points[-1]
        try:
            last_dt = _parse_ts(str(last.get("ts", "")))
            now_dt = _parse_ts(now)
            elapsed = (now_dt - last_dt).total_seconds()
            unchanged = abs(float(last.get("equity", 0)) - equity_val) < 0.001
            if elapsed < 10 and unchanged:
                return history
        except ValueError:
            pass
    points.append(point)

    if len(points) > MAX_POINTS:
        points = points[-MAX_POINTS:]

    payload = {
        "timestamp": now,
        "count": len(points),
        "starting_equity": points[0]["equity"] if points else equity_val,
        "latest_equity": equity_val,
        "points": points,
    }
    write_json_state("equity_history.json", payload)
    return payload


def build_equity_curve(
    paper_orders: dict[str, Any] | None = None,
    paper_trades: dict[str, Any] | None = None,
    account: dict[str, Any] | None = None,
    risk_state: dict[str, Any] | None = None,
    *,
    lite: bool = False,
) -> dict[str, Any]:
    """
    Build equity curve for dashboard.

    Uses live snapshots when available; supplements with trade-based
    reconstruction for closed PnL events.
    """
    paper_orders = paper_orders or read_json_state("paper_orders.json", default={})
    paper_trades = paper_trades or read_json_state("paper_trades.json", default={})
    account = account or read_json_state("account.json", default={})
    risk_state = risk_state or read_json_state("risk_state.json", default={})
    baseline = read_json_state("mt5_baseline.json", default={})
    history = read_json_state("equity_history.json", default={"points": []})
    hist_points = history.get("points", [])
    if lite and len(hist_points) > 180:
        history = {**history, "points": hist_points[-180:]}

    balance = paper_orders.get("balance", {})
    order_account = paper_orders.get("account", {})
    # Prefer the first recorded snapshot equity as the curve baseline.
    # mt5_baseline.starting_cash / paper_orders starting_cash can be stale from
    # an earlier session (was $34 here while the account later grew to $129 via
    # deposits), which detached the trade-close reconstruction from reality and
    # made pnl_total/pnl_pct wildly wrong.
    _first_snap_eq = hist_points[0].get("equity") if hist_points else None
    starting = float(
        _first_snap_eq
        or balance.get("starting_cash")
        or baseline.get("starting_cash")
        or account.get("balance")
        or 1000.0
    )
    current_equity = float(
        account.get("equity")
        or order_account.get("equity")
        or balance.get("equity")
        or account.get("balance")
        or risk_state.get("equity")
        or starting
    )
    current_cash = float(
        account.get("balance")
        or order_account.get("balance")
        or balance.get("cash")
        or current_equity
    )
    unrealized = round(current_equity - current_cash, 2)

    trade_rows = paper_trades.get("trades", [])
    if lite and len(trade_rows) > 40:
        trade_rows = trade_rows[-40:]
    trades = sorted(trade_rows, key=lambda t: t.get("closed_at") or "")

    # LIVE equity curve from real MT5 snapshots: moves in real time with the
    # actual account equity (incl. unrealized PnL of open bot + manual
    # positions). Trade closes are overlaid as markers re-anchored to the
    # nearest snapshot. A light despike drops isolated bad MT5 reads (a single
    # point that spikes far from BOTH neighbours and reverts next tick).
    trade_points: list[dict[str, Any]] = []
    equity = starting
    markers: list[dict[str, Any]] = []
    if trades:
        for trade in trades:
            pnl = float(trade.get("pnl", 0))
            equity += pnl
            ts = trade.get("closed_at") or utc_now_iso()
            markers.append({
                "ts": ts, "equity": round(equity, 2), "event": "trade_close",
                "pnl": round(pnl, 2), "symbol": trade.get("symbol"),
                "side": trade.get("side"), "result": trade.get("result"),
            })

    snapshot_points = [
        {
            "ts": p["ts"],
            "equity": p["equity"],
            "cash": p.get("cash", p.get("balance", p["equity"])),
            "unrealized_pnl": p.get("unrealized_pnl"),
            "drawdown": p.get("drawdown"),
            "event": "snapshot",
            "source": p.get("source"),
        }
        for p in history.get("points", [])
    ]
    if snapshot_points:
        import bisect
        merged = list(snapshot_points)
        _snap_ts = [s["ts"] for s in snapshot_points]
        for m in markers:
            k = bisect.bisect_right(_snap_ts, m["ts"]) - 1
            if 0 <= k < len(snapshot_points):
                m["equity"] = snapshot_points[k]["equity"]
        if len(merged) >= 3:
            _kept = [merged[0]]
            for i in range(1, len(merged) - 1):
                eq = float(merged[i].get("equity", 0))
                pe = float(merged[i - 1].get("equity", 0))
                ne = float(merged[i + 1].get("equity", 0))
                ref = min(pe, ne) if (pe > 0 and ne > 0) else max(pe, ne)
                if ref > 0 and (eq < 0.4 * ref or eq > 2.2 * ref):
                    continue  # isolated bad read (down or up spike that reverts)
                _kept.append(merged[i])
            _kept.append(merged[-1])
            merged = _kept
    elif trade_points:
        merged = _merge_points(trade_points, [])
    else:
        merged = []

    now = utc_now_iso()
    if not merged:
        merged = [{
            "ts": now,
            "equity": round(current_equity, 2),
            "cash": round(current_cash, 2),
            "unrealized_pnl": unrealized,
            "event": "current",
        }]
    elif abs(merged[-1]["equity"] - current_equity) > 0.001 or merged[-1]["ts"][:16] != now[:16]:
        merged.append({
            "ts": now,
            "equity": round(current_equity, 2),
            "cash": round(current_cash, 2),
            "unrealized_pnl": unrealized,
            "drawdown": risk_state.get("drawdown"),
            "event": "current",
        })

    peak = starting
    max_drawdown_pct = 0.0
    enriched: list[dict[str, Any]] = []
    for point in merged:
        eq = float(point["equity"])
        peak = max(peak, eq)
        dd_pct = round((peak - eq) / peak * 100, 2) if peak > 0 else 0.0
        max_drawdown_pct = max(max_drawdown_pct, dd_pct)
        cash = float(point.get("cash", eq))
        enriched.append({
            **point,
            "peak_equity": round(peak, 2),
            "drawdown_pct": dd_pct,
            "unrealized_pnl": point.get("unrealized_pnl", round(eq - cash, 2)),
        })

    # pnl_total reflects live account equity (incl. unrealized) vs the start.
    pnl_total = round(current_equity - starting, 2)
    pnl_pct = round((pnl_total / starting * 100) if starting else 0.0, 2)
    session_start = enriched[0]["equity"] if enriched else starting
    session_pnl = round(current_equity - session_start, 2)

    if lite and len(enriched) > 120:
        enriched = enriched[-120:]
        markers = markers[-30:]

    curve = {
        "starting_equity": round(starting, 2),
        "current_equity": round(current_equity, 2),
        "current_balance": round(current_cash, 2),
        "unrealized_pnl": unrealized,
        "peak_equity": round(max((p["equity"] for p in enriched), default=starting), 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "pnl_total": pnl_total,
        "pnl_pct": pnl_pct,
        "session_pnl": session_pnl,
        "trade_count": len(trades),
        "point_count": len(enriched),
        "points": enriched,
        "markers": markers,
        "range": _curve_range(enriched, starting),
        "ranges": (
            {}
            if lite
            else {key: filter_curve_by_range(enriched, markers, key, starting) for key in EQUITY_RANGE_SECONDS}
        ),
    }
    return curve


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def filter_curve_by_range(
    points: list[dict[str, Any]],
    markers: list[dict[str, Any]],
    range_key: str,
    starting: float,
) -> dict[str, Any]:
    """Slice equity series for dashboard timeframe tabs."""
    seconds = EQUITY_RANGE_SECONDS.get(range_key)
    if not points:
        empty = {
            "key": range_key,
            "points": [],
            "markers": [],
            "point_count": 0,
            "range_stats": {},
        }
        return empty

    now = datetime.now(timezone.utc)
    if seconds is None:
        sliced = list(points)
        sliced_markers = list(markers)
    else:
        cutoff = now - timedelta(seconds=seconds)
        sliced = [p for p in points if _parse_ts(str(p["ts"])) >= cutoff]
        if not sliced:
            sliced = [points[-1]]
        sliced_markers = [m for m in markers if _parse_ts(str(m["ts"])) >= cutoff]

    stats = _range_stats(sliced, starting)
    return {
        "key": range_key,
        "points": sliced,
        "markers": sliced_markers,
        "point_count": len(sliced),
        "range_stats": stats,
    }


def _range_stats(points: list[dict[str, Any]], starting: float) -> dict[str, Any]:
    if not points:
        return {}
    equities = [float(p["equity"]) for p in points]
    balances = [float(p.get("cash", p.get("balance", p["equity"]))) for p in points]
    period_start = equities[0]
    period_end = equities[-1]
    change = round(period_end - period_start, 2)
    change_pct = round((change / period_start * 100) if period_start else 0.0, 2)
    peak = max(equities)
    trough = min(equities)
    max_dd = 0.0
    run_peak = equities[0]
    for eq in equities:
        run_peak = max(run_peak, eq)
        if run_peak > 0:
            max_dd = max(max_dd, (run_peak - eq) / run_peak * 100)
    return {
        "period_start_equity": round(period_start, 2),
        "period_end_equity": round(period_end, 2),
        "period_change": change,
        "period_change_pct": change_pct,
        "period_high": round(peak, 2),
        "period_low": round(trough, 2),
        "period_max_drawdown_pct": round(max_dd, 2),
        "period_min_balance": round(min(balances), 2),
        "period_max_balance": round(max(balances), 2),
        "first_ts": points[0].get("ts"),
        "last_ts": points[-1].get("ts"),
    }


def _curve_range(points: list[dict[str, Any]], starting: float) -> dict[str, Any]:
    if not points:
        return {"min_equity": starting, "max_equity": starting}
    equities = [float(p["equity"]) for p in points]
    balances = [float(p.get("cash", p["equity"])) for p in points]
    return {
        "min_equity": round(min(equities + [starting]), 2),
        "max_equity": round(max(equities + [starting]), 2),
        "min_balance": round(min(balances + [starting]), 2),
        "max_balance": round(max(balances + [starting]), 2),
        "first_ts": points[0].get("ts"),
        "last_ts": points[-1].get("ts"),
    }


def _merge_points(
    trade_points: list[dict[str, Any]],
    snapshot_points: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge trade-derived and snapshot series by timestamp."""
    combined = trade_points + snapshot_points
    if not combined:
        return []
    combined.sort(key=lambda p: p.get("ts", ""))
    merged: list[dict[str, Any]] = []
    seen_minutes: set[str] = set()
    for point in combined:
        minute = str(point.get("ts", ""))[:16]
        if minute in seen_minutes and point.get("event") == "snapshot":
            for i in range(len(merged) - 1, -1, -1):
                if str(merged[i].get("ts", ""))[:16] == minute:
                    merged[i] = point
                    break
            continue
        seen_minutes.add(minute)
        merged.append(point)
    return merged