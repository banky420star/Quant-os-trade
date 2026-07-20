"""Risk management — exposure limits, drawdown, kill switch."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

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
        account_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run all risk checks and produce risk state + kill switch update.

        In mt5 mode, ``account_context`` is required to validate the live account
        before any exposure or drawdown computation. Fail-safe behaviour:

        * ``account_error`` set      -> drawdown forced to 100%, kill switch ON.
        * ``actual != expected``     -> mode mismatch kill switch ON.
        * ``equity <= 0`` on real   -> explicit zero-equity kill switch ON.

        Paper mode ignores account_context entirely so existing tests are unaffected.
        """
        balance = balance or {}
        trades = trades or []
        features_data = features_data or {}
        risk_cfg = self.config["risk"]
        account_context = account_context or {}
        mt5_mode = self.config.get("execution", {}).get("mode") == "mt5"

        starting = float(balance.get("starting_cash", self.config["execution"]["starting_cash"]))
        equity = float(balance.get("equity", starting))

        risk_events: list[dict[str, Any]] = []
        kill_triggers: list[str] = []
        account_status = "ok"
        account_error: str | None = None
        expected_mode: str | None = None
        actual_mode: str | None = None

        if mt5_mode:
            expected_mode = account_context.get("expected_account_mode")
            actual_mode = account_context.get("actual_account_mode")
            account_error = account_context.get("account_error")
            account_status = account_context.get("status", "ok")

            if account_error:
                # Missing/stale/connection_failed: refuse to compute drawdown
                # numbers from fake fallbacks. Force the kill switch on.
                equity = 0.0
                drawdown = 100.0
                risk_events.append({
                    "type": "mt5_account_error",
                    "reason": account_error,
                    "status": account_status,
                })
                kill_triggers.append(f"MT5 account unavailable: {account_error}")
            elif actual_mode == "unknown":
                # Defensive: caller reported status=ok but mode is unknown.
                # Treat as zero-equity so we do not silently fall through to
                # the normal-exposure branch.
                drawdown = 100.0
                risk_events.append({
                    "type": "unknown_account_mode",
                    "expected_mode": expected_mode,
                })
                kill_triggers.append("Unknown MT5 account mode")
            elif (
                expected_mode
                and actual_mode
                and expected_mode != actual_mode
            ):
                drawdown = max(0.0, (starting - equity) / starting * 100) if starting > 0 else 0.0
                risk_events.append({
                    "type": "account_mode_mismatch",
                    "expected": expected_mode,
                    "actual": actual_mode,
                })
                kill_triggers.append(
                    f"Account mode '{actual_mode}' does not match expected '{expected_mode}'"
                )
            elif equity <= 0:
                drawdown = 100.0
                risk_events.append({
                    "type": "zero_equity_real_account",
                    "expected_mode": expected_mode or actual_mode,
                })
                kill_triggers.append("Zero equity on real MT5 account")
            else:
                drawdown = max(0.0, (starting - equity) / starting * 100) if starting > 0 else 0.0
        else:
            drawdown = max(0.0, (starting - equity) / starting * 100) if starting > 0 else 0.0

        symbol_exposure, total_exposure = exposure_from_positions(positions)
        max_total = float(risk_cfg["max_total_exposure_usd"])
        exp_used = exposure_used_pct(total_exposure, max_total)

        kill = dict(existing_kill_switch or {"kill_switch": risk_cfg.get("kill_switch", False), "reason": None, "activated_at": None})

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

        if kill_triggers:
            kill = self._activate_kill_switch(kill, kill_triggers[0])
        elif not risk_cfg.get("kill_switch", False):
            kill = self._clear_kill_switch(kill)

        account_state = {
            "mode": "mt5" if mt5_mode else "paper",
            "status": account_status,
            "expected_account_mode": expected_mode,
            "actual_account_mode": actual_mode,
            "error": account_error,
        }

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
            "account_state": account_state,
        }

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