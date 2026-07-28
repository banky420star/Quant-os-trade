"""Risk management — exposure limits, drawdown, kill switch."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from core.blue_guardian import blue_guardian_enabled, evaluate_daily_state
from core.daily_growth import evaluate_daily_growth
from core.growth_campaign import update_campaign
from core.exposure import exposure_from_positions, exposure_used_pct
from core.micro_profile import independent_symbol_exposure
from core.position_sizing import requires_executable_sizing, resolve_symbol_spec
from core.trade_limits import unlimited_trades
from core.utils import read_json_state, utc_now_iso


class RiskManager:
    """Monitor and enforce portfolio risk limits."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("risk_manager")

    def _resolve_position_specs(
        self,
        positions: list[dict[str, Any]],
        symbol_specs: dict[str, dict[str, float]] | None = None,
    ) -> dict[str, dict[str, float]] | None:
        if not requires_executable_sizing(self.config):
            return None
        specs = dict(symbol_specs or {})
        for pos in positions:
            symbol = pos.get("symbol")
            if symbol and symbol not in specs:
                specs[symbol] = resolve_symbol_spec(symbol, self.config)
        return specs or None

    def evaluate(
        self,
        positions: list[dict[str, Any]],
        orders: list[dict[str, Any]],
        balance: dict[str, Any] | None = None,
        trades: list[dict[str, Any]] | None = None,
        features_data: dict[str, Any] | None = None,
        existing_kill_switch: dict[str, Any] | None = None,
        symbol_specs: dict[str, dict[str, float]] | None = None,
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
        # USER-AUTHORIZED 2026-07-01 fix: the 8% STATIC kill (prop-firm
        # Max/Static Drawdown) is measured from the INITIAL account baseline
        # (mt5_baseline.json starting_cash = $5,000), NOT from
        # daily_growth.day_start_equity. day_start resets every UTC midnight,
        # so across losing days the 8% floor drifted downward and the kill no
        # longer matched the prop-firm "8% static from the $5,000 start" rule.
        # The 4% DAILY-LOSS pause is a separate, daily concept and STILL uses
        # day_start_equity (computed inside daily_growth.evaluate_daily_growth
        # -> trading_paused), so that behaviour is unchanged. Only the static
        # kill's drawdown base moved to the fixed rebaseline anchor.
        baseline_state = read_json_state("mt5_baseline.json", default={})
        drawdown_base = float(baseline_state.get("starting_cash") or starting)
        drawdown = max(0.0, (drawdown_base - equity) / drawdown_base * 100) if drawdown_base > 0 else 0.0

        specs = self._resolve_position_specs(positions, symbol_specs)
        symbol_exposure, total_exposure = exposure_from_positions(
            positions,
            config=self.config,
            symbol_specs=specs,
        )
        max_total = float(risk_cfg["max_total_exposure_usd"])
        exp_used = exposure_used_pct(total_exposure, max_total)
        risk_events: list[dict[str, Any]] = []

        kill = dict(existing_kill_switch or {"kill_switch": risk_cfg.get("kill_switch", False), "reason": None, "activated_at": None})
        kill_triggers: list[str] = []

        if drawdown >= risk_cfg["max_drawdown_pct"]:
            risk_events.append({"type": "max_drawdown", "value": drawdown, "limit": risk_cfg["max_drawdown_pct"]})
            kill_triggers.append(f"Drawdown {drawdown:.2f}% exceeds limit")

        if not unlimited_trades(self.config) and not independent_symbol_exposure(self.config):
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

        bg_state: dict[str, Any] | None = None
        if blue_guardian_enabled(self.config):
            cash = float(balance.get("cash", equity))
            bg_state = evaluate_daily_state(
                self.config,
                balance=cash,
                equity=equity,
            )
            if bg_state.get("trading_paused") and bg_state.get("pause_reason"):
                kill_triggers.append(bg_state["pause_reason"])
            trail_floor = float(bg_state.get("trailing_drawdown_floor", 0) or 0)
            if trail_floor > 0 and equity <= trail_floor:
                kill_triggers.append(
                    f"Blue Guardian trailing DD floor (equity ${equity:.2f} <= ${trail_floor:.2f})"
                )
        elif daily_growth and daily_growth.get("trading_paused") and daily_growth.get("pause_reason"):
            kill_triggers.append(daily_growth["pause_reason"])

        if kill_triggers:
            kill = self._activate_kill_switch(kill, kill_triggers[0])
        else:
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
        if bg_state and bg_state.get("enabled"):
            state["blue_guardian"] = bg_state
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
        """Return loss streak ``limit``; ``None`` disables the kill trigger.

        Disabling rules (priority order):
        * ``unlimited_trades: true`` → ``None``
        * ``risk.disable_consecutive_loss_kill: true`` → ``None``
        * ``practice.growth.max_consecutive_losses`` IS EXPLICITLY SET → use
          that value verbatim. ``0`` disables. This is the OVERRIDE path
          and a profile-level ``0`` wins over any upstream clobbering of
          ``risk.max_consecutive_losses`` (blue_guardian /
          apply_config_overrides / learning_overrides).
        * Otherwise fall back to ``risk.max_consecutive_losses`` (default 5).

        Review fix 2026-07-28: the previous version used ``max()`` of
        both values, which is INVERTED — picking 4 over a profile ``0``
        and letting the kill switch fire again. The correct behaviour is
        "explicit profile value wins when present; otherwise fall back".
        """
        if unlimited_trades(self.config):
            return None
        if bool(risk_cfg.get("disable_consecutive_loss_kill", False)):
            return None
        growth_limit_raw = (self.config.get("practice", {}).get("growth", {}) or {}).get(
            "max_consecutive_losses"
        )
        if isinstance(growth_limit_raw, int) and not isinstance(growth_limit_raw, bool):
            chosen = growth_limit_raw
            return chosen if chosen > 0 else None
        try:
            chosen = int(risk_cfg.get("max_consecutive_losses", 5))
        except (TypeError, ValueError):
            chosen = 5
        return chosen if chosen > 0 else None

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