"""Risk management — exposure limits, drawdown, kill switch."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from core.daily_growth import evaluate_daily_growth
from core.growth_campaign import update_campaign
from core.exposure import exposure_from_positions, exposure_used_pct
from core.trade_limits import unlimited_trades
from core.utils import utc_now_iso


class RiskManager:
    """Monitor and enforce portfolio risk limits."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("risk_manager")

    def evaluate(
        self,
        positions: list[dict[str, Any]],
        orders: list[dict[str, Any]],
        balance: dict[str, Any] | None = None,
        trades: list[dict[str, Any]] | None = None,
        features_data: dict[str, Any] | None = None,
        existing_kill_switch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run all risk checks and produce risk state + kill switch update."""
        balance = balance or {}
        trades = trades or []
        features_data = features_data or {}
        risk_cfg = self.config["risk"]

        starting = float(balance.get("starting_cash", self.config["execution"]["starting_cash"]))
        equity = float(balance.get("equity", starting))
        # USER-AUTHORIZED 2026-06-30 fix: ALWAYS evaluate daily growth so the
        # max-daily-LOSS pause (a safety backstop) is enforced in EVERY mode —
        # previously gated on growth_plan_enabled, which blinded the pause in
        # conservative mode (apply_when=real) and let the bot stack positions
        # into a news spike with no daily-loss circuit-breaker (-57.6% crash).
        # daily_growth.enabled reflects growth-plan status; drawdown_base uses
        # day_start only when enabled (conservative mode keeps the mt5_baseline
        # drawdown base), but trading_paused/pause_reason now run regardless.
        daily_growth = evaluate_daily_growth(equity, self.config)
        drawdown_base = starting
        if daily_growth and daily_growth.get("enabled"):
            drawdown_base = float(daily_growth.get("day_start_equity") or starting)
        drawdown = max(0.0, (drawdown_base - equity) / drawdown_base * 100) if drawdown_base > 0 else 0.0

        symbol_exposure, total_exposure = exposure_from_positions(positions)
        max_total = float(risk_cfg["max_total_exposure_usd"])
        exp_used = exposure_used_pct(total_exposure, max_total)
        risk_events: list[dict[str, Any]] = []

        kill = dict(existing_kill_switch or {"kill_switch": risk_cfg.get("kill_switch", False), "reason": None, "activated_at": None})
        kill_triggers: list[str] = []

        if drawdown >= risk_cfg["max_drawdown_pct"]:
            risk_events.append({"type": "max_drawdown", "value": drawdown, "limit": risk_cfg["max_drawdown_pct"]})
            kill_triggers.append(f"Drawdown {drawdown:.2f}% exceeds limit")

        if not unlimited_trades(self.config):
            if total_exposure > risk_cfg["max_total_exposure_usd"]:
                risk_events.append({"type": "max_total_exposure", "value": total_exposure, "limit": risk_cfg["max_total_exposure_usd"]})

            for symbol, exp in symbol_exposure.items():
                if exp > risk_cfg["max_symbol_exposure_usd"]:
                    risk_events.append({"type": "max_symbol_exposure", "symbol": symbol, "value": exp, "limit": risk_cfg["max_symbol_exposure_usd"]})

        stale = self._stale_orders(orders, risk_cfg["max_order_age_minutes"])
        if stale:
            risk_events.append({"type": "stale_orders", "count": len(stale), "order_ids": [o["order_id"] for o in stale]})

        consecutive_losses = self._consecutive_losses(trades)
        max_losses = self._consecutive_loss_limit(risk_cfg)
        if max_losses is not None and consecutive_losses >= max_losses:
            risk_events.append({"type": "consecutive_losses", "count": consecutive_losses, "limit": max_losses})
            kill_triggers.append(f"{consecutive_losses} consecutive losses")

        bad_vol = self._bad_volatility_symbols(features_data)
        if bad_vol:
            risk_events.append({"type": "bad_volatility", "symbols": bad_vol})

        if daily_growth and daily_growth.get("trading_paused") and daily_growth.get("pause_reason"):
            kill_triggers.append(daily_growth["pause_reason"])

        if kill_triggers:
            kill = self._activate_kill_switch(kill, kill_triggers[0])
        elif not risk_cfg.get("kill_switch", False):
            kill = self._clear_kill_switch(kill)

        state = {
            "timestamp": utc_now_iso(),
            "kill_switch": kill["kill_switch"],
            "risk_events": risk_events,
            "total_exposure": round(total_exposure, 2),
            "symbol_exposure": {k: round(v, 2) for k, v in symbol_exposure.items()},
            "exposure_used_pct": exp_used,
            "max_total_exposure": max_total,
            "max_symbol_exposure": float(risk_cfg["max_symbol_exposure_usd"]),
            "drawdown": round(drawdown, 2),
            "open_positions": len(positions),
            "consecutive_losses": consecutive_losses,
            "equity": round(equity, 2),
            "cash": round(float(balance.get("cash", starting)), 2),
        }
        if daily_growth and daily_growth.get("enabled"):
            state["daily_growth"] = daily_growth
            campaign = update_campaign(equity, self.config)
            if campaign.get("enabled"):
                state["growth_campaign"] = campaign

        self.logger.info(
            "Risk check: exposure=%.2f (%.1f%%) drawdown=%.2f%% positions=%d events=%d kill=%s",
            total_exposure, exp_used, drawdown, len(positions), len(risk_events), kill["kill_switch"],
        )
        return {"risk_state": state, "kill_switch": kill}

    def _stale_orders(self, orders: list[dict[str, Any]], max_age_minutes: int) -> list[dict]:
        stale = []
        now = datetime.now(timezone.utc)
        for order in orders:
            if order.get("status") == "filled":
                continue
            created = order.get("created_at")
            if not created:
                continue
            try:
                ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                continue
            age_min = (now - ts).total_seconds() / 60
            if age_min > max_age_minutes:
                stale.append(order)
        return stale

    def _consecutive_losses(self, trades: list[dict[str, Any]]) -> int:
        if not trades:
            return 0
        sorted_trades = sorted(trades, key=lambda t: t.get("closed_at", ""), reverse=True)
        count = 0
        for trade in sorted_trades:
            if trade.get("result") == "loss":
                count += 1
            else:
                break
        return count

    def _bad_volatility_symbols(self, features_data: dict[str, Any]) -> list[str]:
        bad = []
        for symbol, feat in features_data.get("symbols", {}).items():
            if feat.get("volatility_regime") == "high" and feat.get("atr_ratio", 0) > 0.003:
                bad.append(symbol)
        return bad

    def _consecutive_loss_limit(self, risk_cfg: dict[str, Any]) -> int | None:
        """Return loss streak limit, or None when disabled (0 or unlimited_trades)."""
        if unlimited_trades(self.config):
            return None
        limit = int(risk_cfg.get("max_consecutive_losses", 5))
        return limit if limit > 0 else None

    def _activate_kill_switch(self, kill: dict[str, Any], reason: str) -> dict[str, Any]:
        if not kill.get("kill_switch"):
            self.logger.warning("KILL SWITCH ACTIVATED: %s", reason)
        return {
            "kill_switch": True,
            "reason": reason,
            "activated_at": kill.get("activated_at") or utc_now_iso(),
        }

    def _clear_kill_switch(self, kill: dict[str, Any]) -> dict[str, Any]:
        if kill.get("kill_switch"):
            self.logger.info("Kill switch cleared — risk conditions normalized")
        return {
            "kill_switch": False,
            "reason": None,
            "activated_at": None,
        }