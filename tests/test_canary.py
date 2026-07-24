"""Tests for the symbol canary A/B state machine.

Covers:
- pick_canary: empty / no-trusted / multi-trusted / pinned / pinned-but-untrusted
- tick: init → pick, switch, switch stickiness when promoted,
        promotion (pass/fail), alpha decay, operator-disabled, enabled=false
- lot_multiplier_for: returns 1.0 for non-canary symbols, returns mult for canary,
                      returns 1.0 when deleted/disabled/decayed
- history idempotence: re-running on the same UTC date overwrites
- state-machine invariants: history bounds, idempotence
- audit row schema for every decision path
"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from core import canary
from core.canary import (
    CanaryState,
    STATUS_INIT,
    STATUS_PICKED,
    STATUS_PROMOTED,
    STATUS_DEMOTED,
    STATUS_DECAYED,
    STATUS_DISABLED,
    STATUS_NO_TRUSTED,
    append_audit,
    lot_multiplier_for,
    pick_canary,
    tick,
    _bucket_realized_by_day,
    _check_alpha_decay,
    _realized_daily_series,
    _today_iso_date,
    _upsert_today_history,
)


def _trusted_row(symbol: str, daily: float, expectancy: float = 0.5, n: int = 100) -> dict:
    return {
        "symbol": symbol,
        "n": n,
        "trades_per_day": 5.0,
        "avg_risk_dollar": 0.50,
        "trust": True,
        "trusted": True,
        "seed": {"projected_daily_pnl_usd": 0.0, "expectancy_r": 0.0, "win_rate_pct": 0.0},
        "best": {
            "be_trig": 0.5, "be_lock": 0.1, "tr_act": 0.7, "tr_dist": 0.3,
            "expectancy_r": expectancy, "win_rate_pct": 60.0,
            "ci95_r": [0.1, 0.9],
            "projected_daily_pnl_usd": daily,
            "ci95_daily": [daily * 0.5, daily * 1.5],
        },
        "delta_daily_pnl_usd": daily,
        "trusted": True,
        "reason": "data-driven",
    }


def _closed_trade(symbol: str, pnl: float, days_ago: int) -> dict:
    """Make a fake closed trade `days_ago` UTC days back."""
    from datetime import datetime, timezone, timedelta
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"symbol": symbol, "pnl": pnl, "closed_at": ts.isoformat()}


def _decay_window(older_half_mean: float, newer_half_mean: float,
                  half_std: float = 4.0, half_size: int = 7) -> list[float]:
    """Build a 14-day window split into two halves with non-zero std per half."""
    from datetime import datetime, timezone, timedelta
    older = [older_half_mean + (i - half_size // 2) * (half_std / max(half_size, 1))
             for i in range(half_size)]
    newer = [newer_half_mean + (i - half_size // 2) * (half_std / max(half_size, 1))
             for i in range(half_size)]
    return older + newer  # ascending chronological order


# ---------- pick_canary --------------------------------------------------
class TestPick(unittest.TestCase):
    def test_pick_empty_returns_none(self):
        self.assertIsNone(pick_canary([]))

    def test_pick_no_trusted_returns_none(self):
        rows = [_trusted_row("XAUUSDm", 5.0)]
        rows[0]["trusted"] = False
        self.assertIsNone(pick_canary(rows))

    def test_pick_picks_highest_projection(self):
        rows = [
            _trusted_row("XAUUSDm", 5.0),
            _trusted_row("BTCUSDm", 20.0),
            _trusted_row("USOILm", 1.0),
        ]
        pick = pick_canary(rows)
        self.assertEqual(pick["symbol"], "BTCUSDm")
        self.assertAlmostEqual(pick["expected_daily_usd"], 20.0)
        self.assertFalse(pick["pinned"])

    def test_pick_skips_below_min_n(self):
        rows = [_trusted_row("BTCUSDm", 20.0, n=5)]
        self.assertIsNone(pick_canary(rows, min_trusted_n=30))

    def test_pick_pinned_used_even_if_not_highest(self):
        rows = [
            _trusted_row("XAUUSDm", 30.0),
            _trusted_row("BTCUSDm", 20.0),
        ]
        cfg = {"canary": {"pinned_symbol": "BTCUSDm"}}
        pick = pick_canary(rows, cfg)
        self.assertEqual(pick["symbol"], "BTCUSDm")
        self.assertTrue(pick["pinned"])

    def test_pick_pinned_empty_disables_canary(self):
        rows = [_trusted_row("BTCUSDm", 20.0)]
        cfg = {"canary": {"pinned_symbol": ""}}
        self.assertIsNone(pick_canary(rows, cfg))

    def test_pick_pinned_but_untrusted_returns_none(self):
        rows = [_trusted_row("BTCUSDm", 20.0)]
        rows[0]["trusted"] = False
        cfg = {"canary": {"pinned_symbol": "BTCUSDm"}}
        self.assertIsNone(pick_canary(rows, cfg))


# ---------- tick: lifecycle paths ---------------------------------------
class TestTickInit(unittest.TestCase):
    def test_init_to_pick(self):
        rows = [_trusted_row("BTCUSDm", 20.0)]
        s = CanaryState()
        s, audit = tick(s, rows, realized_trades=[], config={})
        self.assertEqual(s.status, STATUS_PICKED)
        self.assertEqual(s.symbol, "BTCUSDm")
        self.assertEqual(audit["decision"], "pick")
        self.assertEqual(audit["symbol"], "BTCUSDm")
        self.assertIn("audit", audit)
        self.assertEqual(s.history, [])

    def test_no_trusted_init(self):
        rows = []
        s = CanaryState()
        s, audit = tick(s, rows, realized_trades=[], config={})
        self.assertEqual(s.status, STATUS_NO_TRUSTED)
        self.assertIsNone(s.symbol)
        self.assertEqual(audit["decision"], "no_trusted_pick")
        for k in ("ts", "decision", "symbol", "promotion_check", "alpha_decay", "audit"):
            self.assertIn(k, audit)

    def test_disabled_by_operator(self):
        rows = [_trusted_row("BTCUSDm", 20.0)]
        s = CanaryState(status=STATUS_PROMOTED, symbol="BTCUSDm")
        s, audit = tick(
            s, rows, realized_trades=[],
            config={"canary": {"pinned_symbol": ""}},
        )
        self.assertEqual(s.status, STATUS_DISABLED)
        self.assertEqual(audit["decision"], "disabled_by_operator")

    def test_disabled_by_feature_flag(self):
        rows = [_trusted_row("BTCUSDm", 20.0)]
        s = CanaryState(status=STATUS_PROMOTED, symbol="BTCUSDm")
        s, audit = tick(
            s, rows, realized_trades=[],
            config={"canary": {"enabled": False}},
        )
        self.assertEqual(s.status, STATUS_DISABLED)
        self.assertEqual(audit["decision"], "disabled_by_feature_flag")


class TestTickPromotion(unittest.TestCase):
    def _seeded_state_with_history(
        self, n_days: int, *, canary_pnl: float, control_pnl: float,
        symbol: str = "BTCUSDm",
    ) -> CanaryState:
        s = CanaryState(status=STATUS_PICKED, symbol=symbol)
        s.history = [
            {"ts": "2026-07-01", "decision": "continue",
             "canary_pnl_day": canary_pnl, "control_pnl_day": control_pnl,
             "expectancy_r": 0.5, "expected_daily_usd": 10.0}
        ] * n_days
        s.picked_at = "2026-07-01T00:00:00Z"
        return s

    def test_promote_when_canary_beats_control(self):
        rows = [_trusted_row("BTCUSDm", 10.0)]
        s = self._seeded_state_with_history(n_days=7, canary_pnl=10.0, control_pnl=0.0)
        realized = (
            [_closed_trade("BTCUSDm", 10.0, d) for d in range(7)]
            + [_closed_trade("XAUUSDm", 0.0, d) for d in range(7)]
        )
        s, audit = tick(
            s, rows, realized_trades=realized, config={},
        )
        self.assertEqual(s.status, STATUS_PROMOTED)
        self.assertEqual(audit["decision"], "promote")
        self.assertIsNotNone(s.promoted_at)
        for k in ("ts", "decision", "symbol", "promotion_check", "alpha_decay", "audit"):
            self.assertIn(k, audit)
        self.assertTrue(audit["promotion_check"]["eligible"])
        self.assertTrue(audit["promotion_check"]["passed"])

    def test_demote_when_canary_fails_to_beat_control(self):
        rows = [_trusted_row("BTCUSDm", 10.0)]
        s = self._seeded_state_with_history(n_days=7, canary_pnl=0.0, control_pnl=10.0)
        realized = (
            [_closed_trade("BTCUSDm", 0.0, d) for d in range(7)]
            + [_closed_trade("XAUUSDm", 10.0, d) for d in range(7)]
        )
        s, audit = tick(
            s, rows, realized_trades=realized, config={},
        )
        self.assertEqual(s.status, STATUS_DEMOTED)
        self.assertEqual(audit["decision"], "demote")

    def test_continue_before_promotion_window(self):
        rows = [_trusted_row("BTCUSDm", 10.0)]
        s = self._seeded_state_with_history(n_days=3, canary_pnl=10.0, control_pnl=0.0)
        realized = [_closed_trade("BTCUSDm", 10.0, d) for d in range(3)]
        s, audit = tick(
            s, rows, realized_trades=realized, config={},
        )
        self.assertEqual(s.status, STATUS_PICKED)
        self.assertEqual(audit["decision"], "continue")

    def test_alpha_decay_takes_precedence_over_promotion(self):
        """Two 7-day halves, each ~mean diverging > 1σ from projection."""
        rows = [_trusted_row("BTCUSDm", 10.0)]
        s = CanaryState(status=STATUS_PICKED, symbol="BTCUSDm")
        s.history = [
            {"ts": "2026-07-01", "decision": "continue",
             "canary_pnl_day": 5.0, "control_pnl_day": 0.0,
             "expectancy_r": 0.5, "expected_daily_usd": 10.0}
        ] * 10
        # 14-day closed-trade basis: older mean +30 with std ~5, newer mean -25 with std ~5
        realized = []
        for d in range(7, 14):  # older half: diverges positively ~+3σ
            realized.append(_closed_trade("BTCUSDm", 30.0 + (d - 10), d))
        for d in range(0, 7):  # newer half: diverges negatively ~-7σ
            realized.append(_closed_trade("BTCUSDm", -25.0 + (d - 3), d))
        s, audit = tick(
            s, rows, realized_trades=realized, config={},
        )
        self.assertEqual(s.status, STATUS_DECAYED)
        self.assertEqual(audit["decision"], "decay_demote")


class TestTickSwitch(unittest.TestCase):
    def test_switch_when_pick_changes(self):
        rows1 = [_trusted_row("BTCUSDm", 10.0)]
        s, _ = tick(CanaryState(), rows1, realized_trades=[], config={})
        rows2 = [_trusted_row("EURUSDm", 20.0)]  # higher projected
        s2, audit = tick(
            s, rows2, realized_trades=[], config={},
        )
        self.assertEqual(s2.symbol, "EURUSDm")
        self.assertEqual(s2.status, STATUS_PICKED)
        self.assertEqual(audit["decision"], "switch")
        self.assertEqual(audit["previous_symbol"], "BTCUSDm")

    def test_sticky_promoted_protects_from_trinket_swap(self):
        rows = [_trusted_row("BTCUSDm", 10.0), _trusted_row("USOILm", 10.5)]
        s = CanaryState(status=STATUS_PROMOTED, symbol="BTCUSDm")
        s.promoted_at = "2026-07-01T00:00:00Z"
        s.history = [
            {"ts": "2026-07-01", "decision": "promote",
             "canary_pnl_day": 10.0, "control_pnl_day": 0.0,
             "expectancy_r": 0.5, "expected_daily_usd": 10.0}
        ]
        s, audit = tick(
            s, rows, realized_trades=[], config={},
        )
        # Sticky: should stay on BTCUSDm, status remains PROMOTED.
        self.assertEqual(s.symbol, "BTCUSDm")
        self.assertEqual(s.status, STATUS_PROMOTED)
        self.assertEqual(audit["decision"], "sticky_promoted")
        self.assertEqual(audit["audit"]["candidate"], "USOILm")
        self.assertEqual(audit["audit"]["current_canary"], "BTCUSDm")


# ---------- history idempotence -----------------------------------------
class TestHistoryIdempotence(unittest.TestCase):
    def test_rerun_same_day_does_not_inflate_history(self):
        rows = [_trusted_row("BTCUSDm", 10.0)]
        s = CanaryState(status=STATUS_PICKED, symbol="BTCUSDm")
        # Pre-seed with 7 days so the would-be first tick is "eligible"
        s.history = [
            {"ts": "2026-07-15", "decision": "continue",
             "canary_pnl_day": 10.0, "control_pnl_day": 0.0,
             "expectancy_r": 0.5, "expected_daily_usd": 10.0}
        ] * 6
        # Create today's ISO date row anchored on real UTC time.
        from core.utils import utc_now_iso
        from datetime import datetime, timezone, timedelta
        today_iso = datetime.now(timezone.utc).isoformat()
        s.history.append({
            "ts": today_iso, "decision": "continue",
            "canary_pnl_day": 1.0, "control_pnl_day": 0.0,
            "expectancy_r": 0.5, "expected_daily_usd": 10.0,
        })
        length_before = len(s.history)
        # Run tick twice — second run should overwrite today's row.
        realized_a = [_closed_trade("BTCUSDm", 1.0, 0)]
        realized_b = [_closed_trade("BTCUSDm", 2.0, 0)]
        s, _ = tick(s, rows, realized_trades=realized_a, config={})
        mid = len(s.history)
        s, _ = tick(s, rows, realized_trades=realized_b, config={})
        after = len(s.history)
        # Mid grew by 0 (overwrote today's row); final grew by 0 (still today).
        self.assertEqual(mid, length_before)
        self.assertEqual(after, length_before)

    def test_append_helper_distinguishes_overwrite_vs_append(self):
        hist = [{"ts": "2026-07-22T10:00:00Z", "decision": "continue"}]
        result = _upsert_today_history(hist, {"ts": "2026-07-22T15:00:00Z", "decision": "promote"})
        self.assertEqual(result, "overwrote")
        self.assertEqual(hist[-1]["decision"], "promote")
        self.assertEqual(len(hist), 1)
        result2 = _upsert_today_history(hist, {"ts": "2026-07-23T10:00:00Z", "decision": "continue"})
        self.assertEqual(result2, "appended")
        self.assertEqual(len(hist), 2)


# ---------- helper-level coverage --------------------------------------
class TestAlphaDecayHelper(unittest.TestCase):
    def test_no_decay_when_projection_zero(self):
        triggered, _ = _check_alpha_decay([1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6, 7, -7],
                                           projection_daily_usd=0.0)
        self.assertFalse(triggered)

    def test_no_decay_when_window_too_short(self):
        triggered, reason = _check_alpha_decay([1.0] * 4, projection_daily_usd=5.0)
        self.assertFalse(triggered)
        self.assertIn("insufficient", reason)

    def test_no_decay_when_each_half_stable(self):
        # Both halves ~$5, std ~0.5, projection=5 → no divergence.
        window = [4.5, 5.0, 5.5, 4.7, 5.1, 5.3, 4.9, 5.2,
                  4.5, 5.0, 5.5, 4.7, 5.1, 5.3]
        triggered, _ = _check_alpha_decay(window, projection_daily_usd=5.0)
        self.assertFalse(triggered)

    def test_decay_when_halves_diverge_beyond_one_sigma(self):
        window = _decay_window(30.0, -25.0, half_std=4.0, half_size=7)
        triggered, reason = _check_alpha_decay(window, projection_daily_usd=10.0)
        self.assertTrue(triggered)
        self.assertIn("diverged", reason)


class TestDailySeries(unittest.TestCase):
    def test_realized_series_chronological_ascending(self):
        trades = [_closed_trade("BTCUSDm", 1.0, d) for d in range(5)]
        series = _realized_daily_series(trades, "BTCUSDm", days=5)
        self.assertEqual(len(series), 5)
        # Chronologically ascending: oldest is most-ago, newest is 0 days ago.
        self.assertEqual(series[0], 1.0)
        self.assertEqual(series[-1], 1.0)

    def test_realized_series_zero_for_missing_days(self):
        trades = [_closed_trade("BTCUSDm", 5.0, 0)]
        series = _realized_daily_series(trades, "BTCUSDm", days=5)
        self.assertEqual(series[0], 0.0)  # 5 days ago — no trade
        self.assertEqual(series[-1], 5.0)  # today — trade happened

    def test_realized_series_days_zero_returns_empty(self):
        self.assertEqual(_realized_daily_series([], "BTCUSDm", days=0), [])


class TestBucketHelper(unittest.TestCase):
    def test_buckets_by_utc_day(self):
        trades = [_closed_trade("BTCUSDm", 1.0, 0), _closed_trade("BTCUSDm", 2.0, 0)]
        bucket = _bucket_realized_by_day(trades)
        # Should have exactly 1 entry (today's bucket)
        self.assertEqual(len(bucket), 1)
        self.assertEqual(list(bucket.values())[0], 3.0)


# ---------- state-machine invariants ------------------------------------
class TestStateInvariants(unittest.TestCase):
    def test_history_capped_at_90(self):
        rows = [_trusted_row("BTCUSDm", 10.0)]
        s = CanaryState(status=STATUS_PICKED, symbol="BTCUSDm")
        history = [
            {"ts": "2026-07-01", "decision": "continue",
             "canary_pnl_day": 0.0, "control_pnl_day": 0.0,
             "expectancy_r": 0.5, "expected_daily_usd": 10.0}
        ] * 200  # overflow
        s.history = history
        # Build a non-trivial 7-day realized basis to provoke promotion window
        realized = [_closed_trade("BTCUSDm", 1.0, d) for d in range(7)]
        s2, _ = tick(s, rows, realized_trades=realized, config={})
        self.assertLessEqual(len(s2.history), 90)

    def test_idempotence_when_picks_match_and_window_not_eligible(self):
        rows = [_trusted_row("BTCUSDm", 10.0)]
        s = CanaryState(status=STATUS_PICKED, symbol="BTCUSDm")
        s.history = []
        realized = [_closed_trade("BTCUSDm", 1.0, d) for d in range(3)]
        s2, audit = tick(
            s, rows, realized_trades=realized, config={},
        )
        self.assertEqual(s2.status, STATUS_PICKED)
        self.assertEqual(audit["decision"], "continue")
        self.assertEqual(s2.symbol, s.symbol)

    def test_no_realised_data_does_not_demote(self):
        """Empty realised trades + history-eligible window -> KEEP PICKED."""
        rows = [_trusted_row("BTCUSDm", 10.0)]
        s = CanaryState(status=STATUS_PICKED, symbol="BTCUSDm")
        s.history = [
            {"ts": "2026-07-15", "decision": "continue",
             "canary_pnl_day": 0.0, "control_pnl_day": 0.0,
             "expectancy_r": 0.5, "expected_daily_usd": 10.0}
        ] * 7
        realized = []  # no closed trades at all
        s2, audit = tick(
            s, rows, realized_trades=realized, config={},
        )
        self.assertEqual(s2.status, STATUS_PICKED)
        self.assertEqual(audit["decision"], "continue_no_data")
        self.assertIsNone(s2.demoted_at)


# ---------- load_state + append_audit paths -----------------------------
class TestPersistence(unittest.TestCase):
    def test_append_audit_creates_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "canary_audit.jsonl"
            append_audit({"ts": "2026-07-22", "decision": "pick"}, p)
            self.assertTrue(p.exists())
            rows = p.read_text(encoding="utf-8").strip().split("\n")
            self.assertEqual(len(rows), 1)
            obj = json.loads(rows[0])
            self.assertEqual(obj["decision"], "pick")


# ---------- lot_multiplier_for hook -------------------------------------
class TestLotMultiplierHook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_state(self, s: CanaryState):
        p = self.tmp / "canary_state.json"
        p.write_text(json.dumps(s.to_dict()), encoding="utf-8")

    def test_returns_1_when_no_state_file(self):
        m = lot_multiplier_for("BTCUSDm", state_path_arg=self.tmp / "missing.json")
        self.assertEqual(m, 1.0)

    def test_returns_1_for_non_canary_symbol(self):
        self._write_state(CanaryState(status=STATUS_PROMOTED,
                                      symbol="BTCUSDm", lot_multiplier=1.5))
        m = lot_multiplier_for("XAUUSDm", state_path_arg=self.tmp / "canary_state.json")
        self.assertEqual(m, 1.0)

    def test_returns_multiplier_when_symbol_matches(self):
        self._write_state(CanaryState(status=STATUS_PROMOTED,
                                      symbol="BTCUSDm", lot_multiplier=1.6))
        m = lot_multiplier_for("BTCUSDm", state_path_arg=self.tmp / "canary_state.json")
        self.assertAlmostEqual(m, 1.6)

    def test_returns_1_when_status_decayed(self):
        self._write_state(CanaryState(status=STATUS_DECAYED,
                                      symbol="BTCUSDm", lot_multiplier=1.5))
        m = lot_multiplier_for("BTCUSDm", state_path_arg=self.tmp / "canary_state.json")
        self.assertEqual(m, 1.0)

    def test_returns_1_when_pinned_empty(self):
        self._write_state(CanaryState(status=STATUS_PROMOTED,
                                      symbol="BTCUSDm", lot_multiplier=1.5))
        m = lot_multiplier_for("BTCUSDm",
                               state_path_arg=self.tmp / "canary_state.json",
                               config={"canary": {"pinned_symbol": ""}})
        self.assertEqual(m, 1.0)


# ---------- audit-row contract ------------------------------------------
class TestAuditContract(unittest.TestCase):
    def test_pick_audit_row_keys(self):
        rows = [_trusted_row("BTCUSDm", 10.0)]
        _, audit = tick(CanaryState(), rows, realized_trades=[], config={})
        for k in ("ts", "decision", "symbol", "promotion_check", "alpha_decay", "audit"):
            self.assertIn(k, audit)
        self.assertEqual(audit["audit"]["symbol"], "BTCUSDm")

    def test_audit_row_keys_have_no_patch_keys(self):
        """Observe-only contract: no config-patches keys in any decision row."""
        rows = [_trusted_row("BTCUSDm", 10.0)]
        for cfg in [{}, {"canary": {"enabled": False}},
                    {"canary": {"pinned_symbol": ""}}]:
            s = CanaryState()
            _, audit = tick(s, rows, realized_trades=[], config=cfg)
            flat = json.dumps(audit).lower()
            self.assertNotIn("learning_config_overrides", flat)
            self.assertNotIn("config_proposal", flat)
            self.assertNotIn('"patch":', flat)
            self.assertNotIn('"ops":', flat)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
