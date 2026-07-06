"""Signal verification — reject weak or dangerous trades before execution."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from core.blue_guardian import blue_guardian_enabled, entry_gates
from core.exposure import check_exposure_limits
from core.account_mode import performance_gates_active
from core.news_calendar import entry_allowed_by_news, news_context_at
from core.positive_evolution import (
    evaluate_cell_gate,
    positive_evolution_active,
    positive_evolution_settings,
)
from core.dynamic_entry import evaluate_dynamic_entry, symbol_capacity_available
from core.strategy_policy import culturing_cell_key, setup_allowed, symbol_rule, threshold_overrides
from core.entry_staging import touch_and_check
from core.trade_limits import (
    humanize_verifier_failure,
    is_duplicate_position,
    session_trade_capacity_available,
    symbol_reentry_available,
    unlimited_trades,
)
from core.utils import read_json_state, utc_now_iso


class Verifier:
    """Bouncer at the velvet rope — approve or reject candidate signals."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("verifier")
        self.aggressive = bool(config.get("trading", {}).get("aggressive_mode", False))
        filters = config.get("filters", {})
        self.min_volume_ratio = float(filters.get("min_volume_ratio", 0.8))
        if self.aggressive:
            self.min_volume_ratio = 0.0
        self.spread_mult = float(filters.get("spread_mult", 2.0 if self.aggressive else 1.0))
        # Regime-conditional strategy rules (opt-in). When present, the verifier
        # switches min_confidence / min_rr / bias-alignment / skip per market
        # regime, so one rulebook is NOT applied regardless of market state.
        # Absent this block, behaviour is identical to legacy (live bot safe).
        self.regime_overrides = config.get("signals", {}).get("regime_overrides", {}) or {}
        # Win-condition templates (opt-in). Maps setup_type -> {cell_key: stats}
        # (or -> list/set of allowed cell_keys). When present, the verifier only
        # approves a signal if its condition-cell (setup|regime|bias-aligned) is in
        # the allowed set for its setup_type — i.e. only trade under conditions
        # that historically won for that setup. Absent -> allow all (legacy).
        self.win_templates = config.get("signals", {}).get("win_templates", {}) or {}
        self.win_template_strict = bool(config.get("signals", {}).get("win_template_strict", False))
        # USER-AUTHORIZED 2026-06-30 news-blackout gate. The 14:30 UTC (4:30 local)
        # US news spike wiped 4 stacked positions and crashed the account -57.6%
        # because avoid_news was a no-op flag with NO implementation. This blocks
        # new entries within +/- news_blackout_minutes of each scheduled US
        # release slot (UTC HH:MM). Existing positions are untouched (managed by
        # exits/give-back guard); only NEW entries are gated. Opt-in via
        # filters.avoid_news.
        self.avoid_news = bool(filters.get("avoid_news", False))
        self.news_blackout_minutes = float(filters.get("news_blackout_minutes", 10))
        self.news_release_times_utc = list(filters.get("news_release_times_utc", []) or [])
        # Data-driven per-symbol veto (state/symbol_policy_live.json, rewritten
        # each cycle by the forward_test_loop which runs before this loop).
        # Loaded here so _verify_one is safe even if called outside verify_batch;
        # verify_batch refreshes it per run to pick up the latest veto.
        self._live_veto = read_json_state("symbol_policy_live.json", default={}) or {}
        self._positive_evolution = read_json_state("positive_evolution.json", default={}) or {}
        self._positive_evolution_cfg = positive_evolution_settings(config)
        self._reference_time: Any = None

    def _max_open_confidence(self, active_signals: list[dict[str, Any]] | None) -> float | None:
        """Strongest confidence among currently-open positions.

        Open positions are synced from MT5 and don't carry a confidence field,
        so the broker persists {ticket: confidence} to state/position_confidence.json
        on each fill. We cross-reference by ticket against the live open positions
        so stale (closed-position) entries can't inflate the floor. Returns None
        when no open position has a recorded confidence (floor doesn't apply yet).
        """
        if not active_signals:
            return None
        cmap = read_json_state("position_confidence.json", default={}) or {}
        best: float | None = None
        for p in active_signals:
            t = p.get("ticket")
            if t is None:
                continue
            c = cmap.get(str(t))
            if c is None:
                continue
            try:
                cf = float(c)
            except (TypeError, ValueError):
                continue
            if best is None or cf > best:
                best = cf
        return best

    def verify_batch(
        self,
        candidates: list[dict[str, Any]],
        features_data: dict[str, Any],
        active_signals: list[dict[str, Any]] | None = None,
        kill_switch: bool = False,
        spread_data: dict[str, float] | None = None,
        equity: float | None = None,
        balance: float | None = None,
        closed_trades: list[dict[str, Any]] | None = None,
        reference_time: Any = None,
        symbol_specs: dict[str, dict[str, float]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Verify all candidates; return (approved, rejected)."""
        self._reference_time = reference_time
        active_signals = active_signals or []
        closed_trades = closed_trades or []
        spread_data = spread_data or {}
        features = features_data.get("symbols", {})
        if equity is None:
            equity = float(self.config.get("execution", {}).get("starting_cash", 1000))
        if balance is None:
            balance = equity

        approved: list[dict[str, Any]] = []
        self._symbol_specs = symbol_specs
        self._verify_balance = balance
        rejected: list[dict[str, Any]] = []

        # USER-AUTHORIZED 2026-07-01: data-driven per-symbol veto. Loaded once
        # per verify_batch (the veto state file is rewritten each cycle by the
        # forward_test_loop, which runs before this loop in the pipeline). A
        # candidate whose (setup|regime|align|session) cell is in its symbol's
        # vetoed_cells is hard-rejected. Empty/missing -> permissive.
        self._live_veto = read_json_state("symbol_policy_live.json", default={}) or {}
        if positive_evolution_active(self.config):
            self._positive_evolution = read_json_state("positive_evolution.json", default={}) or {}

        for signal in candidates:
            feat = features.get(signal["symbol"], {})
            spread_pts = spread_data.get(signal["symbol"], 0.0)
            result = self._verify_one(
                signal, feat, active_signals, kill_switch, spread_pts, equity, closed_trades,
            )
            if result["approved"]:
                approved.append(result)
            else:
                rejected.append(result)

        self.logger.info("Verified %d signals: %d approved, %d rejected", len(candidates), len(approved), len(rejected))
        return approved, rejected

    def _verify_one(
        self,
        signal: dict[str, Any],
        feat: dict[str, Any],
        active_signals: list[dict[str, Any]],
        kill_switch: bool,
        spread_pts: float,
        equity: float,
        closed_trades: list[dict[str, Any]],
    ) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        failures: list[str] = []

        # --- Regime-conditional strategy (dynamic / flowing) -----------------
        # Read the regime the decision engine stamped on this signal and, if
        # regime_overrides is configured for that regime, switch the gates.
        # No overrides -> eff_* fall back to the global config (legacy path).
        mc = signal.get("market_context", {}) or {}
        regime = mc.get("market_regime") or {}
        regime_primary = regime.get("primary")
        regime_bias = regime.get("bias")
        ov = self.regime_overrides.get(regime_primary, {}) if regime_primary else {}
        eff_min_confidence = ov.get("min_confidence", self.config["signals"]["min_confidence"])
        eff_min_rr = float(ov.get("min_risk_reward", self.config.get("signals", {}).get("min_risk_reward", 1.2)))

        if ov.get("skip"):
            # Regime is explicitly untradeable under this strategy -> reject.
            checks["regime_allowed"] = False
        else:
            checks["regime_allowed"] = True

        if ov.get("bias_aligned") and regime_bias and regime_bias not in ("neutral", "mixed", "none", "", None):
            # Only take trades whose side agrees with the regime's bias.
            want_side = "BUY" if regime_bias in ("bullish", "up") else "SELL"
            checks["regime_bias_aligned"] = (signal.get("side") == want_side)
        else:
            checks["regime_bias_aligned"] = True

        symbol_rule_data = symbol_rule(self.config, signal.get("symbol", ""))
        eff_min_confidence, eff_min_rr = threshold_overrides(
            symbol_rule_data,
            base_confidence=eff_min_confidence,
            base_risk_reward=eff_min_rr,
        )

        # --- Win-condition template gate (only trade under conditions that
        # historically won for this setup_type) -------------------------------
        # cell = setup | regime | bias-aligned-with-side. This is the faithful
        # implementation of "save the conditions where it won, only use those".
        setup_type = signal.get("setup_type") or "unknown"
        checks["symbol_setup_allowed"] = setup_allowed(symbol_rule_data, setup_type)
        bias_aligned_side = (
            (regime_bias in ("bullish", "up") and signal.get("side") == "BUY")
            or (regime_bias in ("bearish", "down") and signal.get("side") == "SELL")
        )
        cell_key = f"{setup_type}|{regime_primary or '?'}|{'align' if bias_aligned_side else 'counter'}"
        tmpl = self.win_templates.get(setup_type)
        if tmpl is not None:
            allowed = tmpl.keys() if isinstance(tmpl, dict) else set(tmpl)
            checks["win_condition_match"] = cell_key in allowed
        elif self.win_template_strict and self.win_templates:
            # Strict mode: reject setups we have no winning template for.
            checks["win_condition_match"] = False
        else:
            checks["win_condition_match"] = True

        # --- Data-driven per-symbol veto -------------------------------------
        # USER-AUTHORIZED 2026-07-01: the forward-test ledger vetoes cells that
        # clean live data proves lose (n >= min_n AND negative realized
        # expectancy AND win-rate below floor). Cell key here MUST match the
        # ledger's culturing_cell_key (setup|regime|align|session) so a vetoed
        # cell maps 1:1 to this candidate. No verdict for the cell -> allow
        # (permissive; keep collecting). This is the "narrower over time,
        # data-driven" narrowing mechanism (manual allowed_setups is off).
        veto_cell = culturing_cell_key(
            setup_type,
            regime_primary,
            regime_bias,
            signal.get("side"),
            mc.get("session"),
        )
        sym_veto = (self._live_veto.get("symbols", {}) or {}).get(signal.get("symbol", ""), {}) or {}
        vetoed_cells = set(sym_veto.get("vetoed_cells", []) or [])
        checks["data_driven_veto"] = veto_cell not in vetoed_cells

        if self._positive_evolution_cfg.get("enabled"):
            pe_ok, _pe_reason = evaluate_cell_gate(
                self.config,
                signal.get("symbol", ""),
                veto_cell,
                self._positive_evolution,
            )
            checks["positive_evolution"] = pe_ok
        else:
            checks["positive_evolution"] = True

        checks["valid_levels"] = self._check_valid_levels(signal)
        if symbol_rule_data.get("preferred_sessions"):
            preferred_sessions = set(symbol_rule_data["preferred_sessions"])
            in_preferred = mc.get("session") in preferred_sessions
            # Preferred sessions are ranking bias, not a hard gate, unless a
            # symbol explicitly opts into strict session filtering.
            if bool(symbol_rule_data.get("strict_sessions", False)):
                checks["preferred_session"] = in_preferred
            else:
                checks["preferred_session"] = True
                checks["preferred_session_bias"] = in_preferred
        else:
            checks["preferred_session"] = True
        checks["confidence"] = signal.get("confidence", 0) >= eff_min_confidence
        practice_relaxed = (
            not performance_gates_active(self.config)
            and bool(self.config.get("practice", {}).get("relax_structure_checks"))
        )
        if self.aggressive or practice_relaxed:
            checks["timeframe_alignment"] = True
            checks["not_buying_resistance"] = True
            checks["not_selling_support"] = True
        else:
            checks["timeframe_alignment"] = self._check_timeframe_alignment(signal, feat)
            checks["not_buying_resistance"] = self._check_not_buying_resistance(signal, feat)
            checks["not_selling_support"] = self._check_not_selling_support(signal, feat)
        checks["spread_safe"] = self._check_spread(signal["symbol"], spread_pts)
        checks["atr_safe"] = feat.get("atr_ratio", 0) >= self.config["filters"]["min_atr_ratio"]
        practice_relaxed = (
            not performance_gates_active(self.config)
            and bool(self.config.get("practice", {}).get("relax_volume_check"))
        )
        if self.aggressive or practice_relaxed:
            checks["volume_safe"] = True
        else:
            checks["volume_safe"] = feat.get("volume_ratio", 0) >= self.min_volume_ratio
        min_rr = eff_min_rr
        checks["risk_reward_safe"] = self._check_risk_reward(signal, min_rr=min_rr)
        if unlimited_trades(self.config):
            exposure_ok, exposure_details = True, {"unlimited_trades": True}
            checks["exposure_safe"] = True
        else:
            exposure_ok, exposure_details = check_exposure_limits(
                active_signals,
                signal,
                equity,
                self.config,
                balance=getattr(self, "_verify_balance", equity),
                symbol_specs=getattr(self, "_symbol_specs", None),
            )
            checks["exposure_safe"] = exposure_ok
            if not exposure_ok:
                failures.append("exposure_limit_exceeded")

        checks["no_duplicate"] = not is_duplicate_position(
            self.config,
            signal,
            active_signals,
        )
        checks["symbol_capacity"] = symbol_capacity_available(
            self.config,
            signal["symbol"],
            active_signals,
        )
        # Confidence floor: a new position must be at least as confident as the
        # strongest currently-open position (don't add a weaker trade on top of
        # a stronger one). Open-position confidences are persisted by the broker
        # to state/position_confidence.json (keyed by MT5 ticket); positions
        # opened before this feature had none, so the floor only applies once a
        # confident position is on the books. Opt-in via trading.confidence_floor_vs_open.
        if bool(self.config.get("trading", {}).get("confidence_floor_vs_open", False)):
            sig_conf = float(signal.get("confidence", 0) or 0)
            open_conf = self._max_open_confidence(active_signals)
            checks["confidence_floor"] = (open_conf is None) or (sig_conf >= open_conf - 0.01)
        else:
            checks["confidence_floor"] = True
        checks["session_trade_capacity"] = session_trade_capacity_available(
            self.config,
            signal["symbol"],
            closed_trades,
        )
        reentry_ok, _reentry_wait = symbol_reentry_available(
            self.config, signal["symbol"], closed_trades,
        )
        checks["reentry_cooldown"] = reentry_ok
        confirm = touch_and_check(signal, self.config, feat=feat)
        checks["entry_confirm"] = bool(confirm.get("ready"))
        dyn_ok, adjusted_signal, dyn_reason = evaluate_dynamic_entry(
            self.config,
            signal,
            active_signals,
            feat,
        )
        checks["dynamic_entry"] = dyn_ok
        if not dyn_ok and dyn_reason:
            failures.append(dyn_reason)

        checks["kill_switch_safe"] = not kill_switch
        checks["news_safe"] = self._check_news_blackout()
        news_ctx = news_context_at(self._reference_time, self.config)
        checks["macro_news_safe"] = bool(news_ctx.get("safe_for_entry", True))
        if not checks["macro_news_safe"]:
            checks["news_safe"] = False
        bg_code = None
        if blue_guardian_enabled(self.config):
            bg_ok, bg_code, _bg_details = entry_gates(self.config, active_signals, signal)
            checks["blue_guardian_entry"] = bg_ok
        else:
            checks["blue_guardian_entry"] = True

        failure_codes: list[str] = []
        for name, passed in checks.items():
            if passed:
                continue
            if name == "preferred_session_bias":
                continue
            if name == "blue_guardian_entry" and bg_code:
                failure_codes.append(bg_code)
            elif name == "exposure_safe":
                failure_codes.append("exposure_limit_exceeded")
            elif name == "symbol_setup_allowed":
                failure_codes.append("symbol_setup_restricted")
            elif name == "preferred_session":
                failure_codes.append("session_misaligned")
            elif name == "data_driven_veto":
                failure_codes.append("data_driven_veto")
            elif name == "positive_evolution":
                failure_codes.append("positive_evolution")
            else:
                failure_codes.append(name)

        failures = [
            humanize_verifier_failure(
                code,
                signal,
                self.config,
                active_positions=active_signals,
                closed_trades=closed_trades,
            )
            for code in failure_codes
        ]

        approved = len(failures) == 0
        record = {
            "signal_id": signal["signal_id"],
            "symbol": signal["symbol"],
            "side": signal.get("side"),
            "setup_type": signal.get("setup_type"),
            "approved": approved,
            "checks": checks,
            "failure_codes": failure_codes,
            "failures": failures,
            "spread_points": spread_pts,
            "confidence": signal.get("confidence"),
            "reason": signal.get("reason"),
            "verified_at": utc_now_iso(),
            "exposure": exposure_details,
        }
        if approved:
            record["approved_at"] = utc_now_iso()
            record["signal"] = adjusted_signal if dyn_ok else signal
        else:
            record["rejected_at"] = utc_now_iso()
            record["rejection_reason"] = "; ".join(failures)

        return record

    def _check_valid_levels(self, signal: dict[str, Any]) -> bool:
        entry = signal.get("entry")
        sl = signal.get("sl")
        tp1 = signal.get("tp1")
        if not all(isinstance(v, (int, float)) and v > 0 for v in (entry, sl, tp1)):
            return False
        if signal.get("side") == "BUY":
            return sl < entry < tp1
        return tp1 < entry < sl

    def _check_timeframe_alignment(self, signal: dict[str, Any], feat: dict[str, Any]) -> bool:
        m5 = feat.get("m5_trend", "neutral")
        m15 = feat.get("m15_trend", "neutral")
        if m5 == "bullish" and m15 == "bearish" and signal.get("side") == "BUY":
            return False
        if m5 == "bearish" and m15 == "bullish" and signal.get("side") == "SELL":
            return False
        return True

    def _check_not_buying_resistance(self, signal: dict[str, Any], feat: dict[str, Any]) -> bool:
        if signal.get("side") != "BUY":
            return True
        price = signal.get("entry", feat.get("price", 0))
        resistance = feat.get("resistance", price * 1.01)
        return abs(resistance - price) / price > 0.001 if price else True

    def _check_not_selling_support(self, signal: dict[str, Any], feat: dict[str, Any]) -> bool:
        if signal.get("side") != "SELL":
            return True
        price = signal.get("entry", feat.get("price", 0))
        support = feat.get("support", price * 0.99)
        return abs(price - support) / price > 0.001 if price else True

    def _check_spread(self, symbol: str, spread_pts: float) -> bool:
        max_spread = float(self.config["filters"]["max_spread_points"].get(symbol, 999))
        return spread_pts <= max_spread * self.spread_mult

    def _check_risk_reward(self, signal: dict[str, Any], min_rr: float = 1.2) -> bool:
        entry = signal.get("entry", 0)
        sl = signal.get("sl", 0)
        tp1 = signal.get("tp1", 0)
        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        if risk <= 0:
            return False
        return (reward / risk) >= min_rr

    def _check_news_blackout(self) -> bool:
        """Block new entries within +/- news_blackout_minutes of scheduled US
        release times (UTC). Uses reference_time during replay, else live clock.
        """
        when = self._reference_time if self._reference_time is not None else datetime.now(timezone.utc)
        return entry_allowed_by_news(when, self.config)

