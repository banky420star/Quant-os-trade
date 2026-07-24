"""Tests for the live edge monitor (core/edge_monitor.py).

Covers:
  * ENTRY conditions (E1..E5): HALT only when both halves diverge > 2σ AND
    projection > 0 AND window is full AND not already halted. Zero-variance
    half means still trip the trigger when off-projection > $0.50.
  * EXIT conditions (X1..X3, dropped X4 after the COOLING_OFF phase was
    simplified out): recovery requires >= 5 consecutive in-band observations.
  * WATCH status: single-half divergence from OK sets WATCH (advisory);
    back to OK on next in-band observation.
  * Audit-row observe-only invariant: writes_blocked list exhaustively
    enumerates NEVER-written targets; no patch-style keys in any audit row.
  * is_halted / current_status query hooks.
  * Idempotence (same-day re-runs overwrite today's history row).
  * Persistence round-trip via tmpfile JSON.
  * Bulk tick() against mixed trusted/untrusted rows from verify_edge.
"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from core import edge_monitor as em
from core.edge_monitor import (
    EDGE_BLOCKED_TARGETS,
    HALT_DECAY_WINDOW_DAYS,
    HALT_HALF_WINDOW_DAYS,
    HALT_RECOVERY_REQUIRED_DAYS,
    HALT_SIGMA_THRESHOLD,
    STATUS_COOLING_OFF,
    STATUS_HALT,
    STATUS_OK,
    STATUS_WATCH,
    SymbolEdgeState,
    _half_divergence,
    _realized_daily_series,
    _upsert_today_history,
    _window_has_real_data,
    append_audit,
    current_status,
    evaluate_symbol,
    is_halted,
    load_state,
    save_state,
    tick,
)


# ---------- helpers -----------------------------------------------------
def _closed_trade(symbol: str, pnl: float, days_ago: int) -> dict:
    from datetime import datetime, timezone, timedelta
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"symbol": symbol, "pnl": pnl, "closed_at": ts.isoformat()}


def _build_realized(symbol: str, pnls_by_day_ago: dict[int, float]) -> list[dict]:
    return [
        _closed_trade(symbol, pnl, days_ago)
        for days_ago, pnl in pnls_by_day_ago.items()
    ]


def _audit_search_blob(audit_row: dict) -> str:
    """Return the audit-row JSON WITHOUT the writes_blocked field.

    Exempting writes_blocked lets the row legitimately list the file
    names that the observe-only contract blocks (so a future operator
    query can read ``audit['writes_blocked']`` and tune accordingly)
    while still flagging any other occurrence of those tokens anywhere
    else in the row.
    """
    scrubbed = {k: v for k, v in audit_row.items() if k != "writes_blocked"}
    return json.dumps(scrubbed, default=str).lower()


# ---------- half-divergence primitive ----------------------------------
class TestHalfDivergence(unittest.TestCase):
    def test_zero_projection_returns_no_divergence(self):
        d = _half_divergence([1.0] * 7, projection=0.0)
        self.assertFalse(d["diverges"])
        self.assertEqual(d["reason"], "projection_zero")

    def test_zero_projection_explicit_no_divergence(self):
        # E1's guard intercepts this at evaluate_symbol level; _half_divergence
        # itself should still mark non-divergent for projection=0.
        d = _half_divergence([5.0] * 7, projection=0.0)
        self.assertFalse(d["diverges"])

    def test_too_short_half_returns_no_divergence(self):
        d = _half_divergence([1.0], projection=10.0)
        self.assertFalse(d["diverges"])
        self.assertEqual(d["reason"], "insufficient_sample")

    def test_zero_variance_close_to_projection_no_diverge(self):
        # mean=10, std=0, projection=10 → diff=0 → no divergence (within $0.50 floor)
        d = _half_divergence([10.0] * 7, projection=10.0)
        self.assertFalse(d["diverges"])
        self.assertEqual(d["reason"], "zero_variance_deterministic")

    def test_zero_variance_far_from_projection_does_diverge(self):
        # mean=30, std=0, projection=10 → diff=20 > 0.5 floor → DIVERGE
        d = _half_divergence([30.0] * 7, projection=10.0)
        self.assertTrue(d["diverges"])
        self.assertEqual(d["reason"], "zero_variance_deterministic")

    def test_normal_divergence_above_two_sigma(self):
        # mean=20, std=2, projection=10 → |10| > 4 → diverge
        d = _half_divergence([19, 20, 21, 19, 20, 21, 20], projection=10.0)
        self.assertTrue(d["diverges"])
        self.assertEqual(d["reason"], "evaluated")

    def test_one_sigma_band_does_not_diverge(self):
        # mean=10.4, std=1.5, projection=10 → |0.4| > 3? No.
        d = _half_divergence([10, 11, 12, 9, 10, 11, 10], projection=10.0)
        self.assertFalse(d["diverges"])
        # Sanity check: a sequence that IS within 2σ of a different projection.
        # mean=10, std=2, projection=15 → |5| > 4? Yes, diverges.
        d2 = _half_divergence([8, 9, 10, 11, 12, 9, 11], projection=15.0)
        self.assertTrue(d2["diverges"])


# ---------- ENTRY conditions --------------------------------------------
class TestEntryConditions(unittest.TestCase):
    def test_E1_zero_projection_emits_no_data(self):
        realized = _build_realized("BTCUSDm", {d: 30.0 for d in range(14)})
        s = SymbolEdgeState(symbol="BTCUSDm")
        s2, audit = evaluate_symbol("BTCUSDm", projection_daily_usd=0.0,
                                    realized_trades=realized, state=s)
        self.assertEqual(s2.status, STATUS_OK)
        self.assertEqual(audit["decision"], "no_data")

    def test_E1_negative_projection_emits_no_data(self):
        realized = _build_realized("BTCUSDm", {d: 30.0 for d in range(14)})
        s = SymbolEdgeState(symbol="BTCUSDm")
        s2, audit = evaluate_symbol("BTCUSDm", projection_daily_usd=-1.0,
                                    realized_trades=realized, state=s)
        self.assertEqual(audit["decision"], "no_data")

    def test_E2_insufficient_data_does_not_halt(self):
        realized = _build_realized("BTCUSDm", {0: 30.0})
        s = SymbolEdgeState(symbol="BTCUSDm")
        s2, _ = evaluate_symbol("BTCUSDm", projection_daily_usd=10.0,
                                realized_trades=realized, state=s)
        self.assertEqual(s2.status, STATUS_OK)

    def test_E3_single_half_diverge_sets_watch(self):
        # newer half (0..6 days ago) all 30 → diverges deterministically;
        # older half (7..13 days ago) all 10 → not divergence.
        realized = _build_realized(
            "BTCUSDm",
            {d: 30.0 for d in range(0, 7)} | {d: 10.0 for d in range(7, 14)},
        )
        s = SymbolEdgeState(symbol="BTCUSDm")
        s2, audit = evaluate_symbol("BTCUSDm", projection_daily_usd=10.0,
                                    realized_trades=realized, state=s)
        self.assertEqual(s2.status, STATUS_WATCH)
        self.assertEqual(audit["decision"], "watch_entered")

    def test_E4_E5_two_halves_diverge_halts(self):
        realized = _build_realized(
            "BTCUSDm",
            {d: 30.0 + (d - 13) for d in range(7, 14)}
            | {d: -25.0 + (d - 3) for d in range(0, 7)},
        )
        s = SymbolEdgeState(symbol="BTCUSDm")
        s2, audit = evaluate_symbol("BTCUSDm", projection_daily_usd=10.0,
                                    realized_trades=realized, state=s)
        self.assertEqual(s2.status, STATUS_HALT)
        self.assertEqual(audit["decision"], "halt_entered")
        self.assertEqual(audit["previous_status"], STATUS_OK)
        self.assertEqual(audit["new_status"], STATUS_HALT)
        self.assertIsNotNone(s2.halt_entered_at)
        self.assertEqual(s2.halt_entered_count, 1)

    def test_already_halted_does_not_double_count(self):
        # Continue observing divergent data; status must stay HALT,
        # count must NOT increment.
        realized = _build_realized(
            "BTCUSDm",
            {d: 30.0 for d in range(7, 14)} | {d: -25.0 for d in range(0, 7)},
        )
        s = SymbolEdgeState(symbol="BTCUSDm", status=STATUS_HALT,
                            halt_entered_at="2026-07-01T00:00:00Z",
                            halt_entered_count=1)
        s2, audit = evaluate_symbol("BTCUSDm", projection_daily_usd=10.0,
                                    realized_trades=realized, state=s)
        self.assertEqual(s2.status, STATUS_HALT)
        self.assertEqual(s2.halt_entered_count, 1)
        # Both halves still diverge → no in_band branch → fallback decision.
        self.assertEqual(audit["decision"], "evaluated_no_state_change")


# ---------- EXIT / recovery conditions ---------------------------------
class TestExitConditions(unittest.TestCase):
    def _halted_state(self) -> SymbolEdgeState:
        return SymbolEdgeState(
            symbol="BTCUSDm",
            status=STATUS_HALT,
            halt_entered_at="2026-07-01T00:00:00Z",
            halt_entered_count=1,
            consecutive_in_band=0,
        )

    def test_X1_X3_five_consecutive_in_band_returns_to_ok(self):
        in_band = _build_realized(
            "BTCUSDm",
            {d: 10.0 for d in range(7)} | {d: 10.0 for d in range(7, 14)},
        )
        s = self._halted_state()
        # Same-day rollover: force last_in_band_day to advance on each tick
        # so the recovery counter actually accumulates to the threshold
        # across 5 distinct in-band days.
        for i in range(HALT_RECOVERY_REQUIRED_DAYS):
            s.last_in_band_day = f"2026-07-0{i + 1}" if i > 0 else None
            s, audit = evaluate_symbol(
                "BTCUSDm", projection_daily_usd=10.0,
                realized_trades=in_band, state=s,
            )
        self.assertEqual(s.status, STATUS_OK)
        self.assertEqual(s.consecutive_in_band, HALT_RECOVERY_REQUIRED_DAYS)
        self.assertEqual(audit["decision"], "ok_resumed")

    def test_short_in_band_run_does_not_clear_halt(self):
        in_band = _build_realized(
            "BTCUSDm",
            {d: 10.0 for d in range(7)} | {d: 10.0 for d in range(7, 14)},
        )
        divergent = _build_realized(
            "BTCUSDm",
            {d: 30.0 for d in range(7, 14)} | {d: -25.0 for d in range(0, 7)},
        )
        s = self._halted_state()
        # 4 in-band ticks on 4 distinct UTC days (force the day rollover)
        for i in range(HALT_RECOVERY_REQUIRED_DAYS - 1):
            s.last_in_band_day = f"2026-07-0{i + 1}" if i > 0 else None
            s, _ = evaluate_symbol(
                "BTCUSDm", projection_daily_usd=10.0,
                realized_trades=in_band, state=s,
            )
        self.assertEqual(s.status, STATUS_HALT)
        # Divergent tick resets streak but stays HALT (no double-count).
        s, audit = evaluate_symbol(
            "BTCUSDm", projection_daily_usd=10.0,
            realized_trades=divergent, state=s,
        )
        self.assertEqual(s.status, STATUS_HALT)
        self.assertEqual(s.halt_entered_count, 1)
        self.assertEqual(audit["decision"], "evaluated_no_state_change")

    def test_recovery_resets_consecutive_on_break(self):
        in_band = _build_realized(
            "BTCUSDm",
            {d: 10.0 for d in range(7)} | {d: 10.0 for d in range(7, 14)},
        )
        divergent = _build_realized(
            "BTCUSDm",
            {d: 30.0 for d in range(7, 14)} | {d: -25.0 for d in range(0, 7)},
        )
        s = self._halted_state()
        # 3 in-band on distinct days, then 1 divergent (resets streak),
        # then 5 in-band on distinct days to OK.
        for i in range(3):
            s.last_in_band_day = f"2026-07-0{i + 5}" if i > 0 else None
            s, _ = evaluate_symbol("BTCUSDm", projection_daily_usd=10.0,
                                   realized_trades=in_band, state=s)
        s, _ = evaluate_symbol("BTCUSDm", projection_daily_usd=10.0,
                               realized_trades=divergent, state=s)
        self.assertEqual(s.consecutive_in_band, 0)
        for i in range(HALT_RECOVERY_REQUIRED_DAYS):
            s.last_in_band_day = f"2026-07-1{i + 5}" if i > 0 else None
            s, _ = evaluate_symbol("BTCUSDm", projection_daily_usd=10.0,
                                   realized_trades=in_band, state=s)
        self.assertEqual(s.status, STATUS_OK)

    def test_watch_to_ok_resumes_on_in_band(self):
        s = SymbolEdgeState(symbol="BTCUSDm", status=STATUS_WATCH)
        in_band = _build_realized("BTCUSDm",
                                  {d: 10.0 for d in range(0, 14)})
        s2, audit = evaluate_symbol("BTCUSDm", projection_daily_usd=10.0,
                                    realized_trades=in_band, state=s)
        self.assertEqual(s2.status, STATUS_OK)
        self.assertEqual(audit["decision"], "ok_resumed_from_watch")


# ---------- Audit-row observe-only invariant ----------------------------
class TestAuditInvariant(unittest.TestCase):
    def setUp(self):
        self.realized_divergent = _build_realized(
            "BTCUSDm",
            {d: 30.0 + (d - 13) for d in range(7, 14)}
            | {d: -25.0 + (d - 3) for d in range(0, 7)},
        )

    def test_audit_payload_has_no_patch_keys_outside_writes_blocked(self):
        """Outside the writes_blocked list, the audit row carries no
        config-patch surface (no patches, ops, overrides, proposals)."""
        s = SymbolEdgeState(symbol="BTCUSDm")
        _, audit = evaluate_symbol("BTCUSDm", projection_daily_usd=10.0,
                                   realized_trades=self.realized_divergent,
                                   state=s)
        flat = _audit_search_blob(audit)
        for forbidden in ('"patch"', '"ops"', "config_overrides"):
            self.assertNotIn(forbidden.lower(), flat,
                             f"audit row leaked forbidden token {forbidden}")

    def test_audit_writes_blocked_list_covers_all_targets(self):
        s = SymbolEdgeState(symbol="BTCUSDm")
        _, audit = evaluate_symbol("BTCUSDm", projection_daily_usd=10.0,
                                   realized_trades=self.realized_divergent,
                                   state=s)
        self.assertIn("writes_blocked", audit)
        self.assertIsInstance(audit["writes_blocked"], list)
        # Exhaustive coverage of every entry in the contract.
        for target in EDGE_BLOCKED_TARGETS:
            self.assertIn(target, audit["writes_blocked"],
                          f"{target} missing from writes_blocked")

    def test_states_pure_round_trip_no_config_write(self):
        """The audit row written through append_audit() never porks config."""
        with tempfile.TemporaryDirectory() as td:
            audit_p = Path(td) / "edge_monitor_audit.jsonl"
            s = SymbolEdgeState(symbol="BTCUSDm")
            _, audit = evaluate_symbol("BTCUSDm", projection_daily_usd=10.0,
                                       realized_trades=self.realized_divergent,
                                       state=s)
            append_audit(audit, audit_p)
            content = audit_p.read_text(encoding="utf-8")
            # The writes_blocked field legitimately carries the file names,
            # so we read the row and check ALL fields except writes_blocked.
            row = json.loads(content.strip().splitlines()[0])
            audit_minus_blocklist = {
                k: v for k, v in row.items() if k != "writes_blocked"
            }
            flat = json.dumps(audit_minus_blocklist).lower()
            for forbidden in ('"patch"', '"ops"', "config_overrides",
                              "learning_config_overrides", "config_proposals"):
                self.assertNotIn(forbidden, flat,
                                 f"audit field outside writes_blocked leaked {forbidden}")


# ---------- is_halted / current_status hooks ---------------------------
class TestQueryHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_state(self, doc):
        (self.tmp / "edge_monitor_state.json").write_text(
            json.dumps(doc), encoding="utf-8"
        )

    def test_is_halted_true_when_status_halt(self):
        self._write_state({"BTCUSDm": {"status": STATUS_HALT}})
        self.assertTrue(is_halted("BTCUSDm",
                                  state_path_arg=self.tmp / "edge_monitor_state.json"))

    def test_is_halted_true_when_status_cooling_off(self):
        self._write_state({"BTCUSDm": {"status": STATUS_COOLING_OFF}})
        self.assertTrue(is_halted("BTCUSDm",
                                  state_path_arg=self.tmp / "edge_monitor_state.json"))

    def test_is_halted_false_when_status_ok(self):
        self._write_state({"BTCUSDm": {"status": STATUS_OK}})
        self.assertFalse(is_halted("BTCUSDm",
                                   state_path_arg=self.tmp / "edge_monitor_state.json"))

    def test_is_halted_false_when_status_watch(self):
        self._write_state({"BTCUSDm": {"status": STATUS_WATCH}})
        self.assertFalse(is_halted("BTCUSDm",
                                   state_path_arg=self.tmp / "edge_monitor_state.json"))

    def test_is_halted_false_when_file_missing(self):
        self.assertFalse(is_halted("BTCUSDm",
                                   state_path_arg=self.tmp / "missing.json"))

    def test_is_halted_false_when_corrupt_json(self):
        p = self.tmp / "edge_monitor_state.json"
        p.write_text("{not-json", encoding="utf-8")
        self.assertFalse(is_halted("BTCUSDm", state_path_arg=p))

    def test_current_status_returns_status_or_ok(self):
        self._write_state({"BTCUSDm": {"status": STATUS_WATCH}})
        self.assertEqual(current_status(
            "BTCUSDm", state_path_arg=self.tmp / "edge_monitor_state.json"),
            STATUS_WATCH)
        self.assertEqual(current_status("XAUUSDm",
                                        state_path_arg=self.tmp / "edge_monitor_state.json"),
                         STATUS_OK)


# ---------- Bulk tick ----------------------------------------------------
class TestBulkTick(unittest.TestCase):
    def test_tick_processes_listed_projection_symbols(self):
        rows = [
            {"symbol": "BTCUSDm", "best": {"projected_daily_pnl_usd": 10.0}, "trusted": True},
            {"symbol": "XAUUSDm", "best": {"projected_daily_pnl_usd": 5.0}, "trusted": False},
        ]
        divergent = _build_realized(
            "BTCUSDm",
            {d: 30.0 + (d - 13) for d in range(7, 14)}
            | {d: -25.0 + (d - 3) for d in range(0, 7)},
        )
        state_doc, audits = tick(
            rows, realized_trades=divergent, state_doc=None, config={},
        )
        self.assertIn("BTCUSDm", state_doc)
        self.assertIn("XAUUSDm", state_doc)
        # BTCUSDm has divergent realised → HALT. XAUUSDm has positive
        # projection but no realised data → no_data decision (status stays OK).
        self.assertEqual(state_doc["BTCUSDm"]["status"], STATUS_HALT)
        self.assertEqual(state_doc["XAUUSDm"]["status"], STATUS_OK)
        self.assertEqual(len(audits), 2)

    def test_tick_sorts_symbols_alphabetically(self):
        rows = [
            {"symbol": "XAUUSDm", "best": {"projected_daily_pnl_usd": 5.0}},
            {"symbol": "BTCUSDm", "best": {"projected_daily_pnl_usd": 10.0}},
            {"symbol": "EURUSDm", "best": {"projected_daily_pnl_usd": 1.0}},
        ]
        _, audits = tick(rows, realized_trades=[], state_doc=None, config={})
        ordered_symbols = [a["symbol"] for a in audits]
        self.assertEqual(ordered_symbols, sorted(ordered_symbols))

    def test_tick_carries_state_forward_persists_count(self):
        rows = [{"symbol": "BTCUSDm", "best": {"projected_daily_pnl_usd": 10.0}}]
        divergent = _build_realized(
            "BTCUSDm",
            {d: 30.0 + (d - 13) for d in range(7, 14)}
            | {d: -25.0 + (d - 3) for d in range(0, 7)},
        )
        state_doc, _ = tick(rows, realized_trades=divergent,
                            state_doc=None, config={})
        self.assertEqual(state_doc["BTCUSDm"]["status"], STATUS_HALT)
        self.assertEqual(state_doc["BTCUSDm"]["halt_entered_count"], 1)
        # Re-running with same divergent data: count stays at 1.
        state_doc2, _ = tick(rows, realized_trades=divergent,
                             state_doc=state_doc, config={})
        self.assertEqual(state_doc2["BTCUSDm"]["status"], STATUS_HALT)
        self.assertEqual(state_doc2["BTCUSDm"]["halt_entered_count"], 1)


# ---------- Idempotence / history --------------------------------------
class TestHistoryIdempotence(unittest.TestCase):
    def test_rerun_same_day_overwrites_history(self):
        s = SymbolEdgeState(symbol="BTCUSDm")
        in_band = _build_realized("BTCUSDm",
                                  {d: 10.0 for d in range(0, 14)})
        s, _ = evaluate_symbol("BTCUSDm", projection_daily_usd=10.0,
                               realized_trades=in_band, state=s)
        length_after_first = len(s.history)
        s, _ = evaluate_symbol("BTCUSDm", projection_daily_usd=10.0,
                               realized_trades=in_band, state=s)
        self.assertEqual(len(s.history), length_after_first)

    def test_history_helper_overwrite_vs_append(self):
        hist = [{"ts": "2026-07-22T10:00:00Z", "decision": "ok"}]
        result = _upsert_today_history(hist,
                                       {"ts": "2026-07-22T15:00:00Z", "decision": "halt_entered"})
        self.assertEqual(result, "overwrote")
        self.assertEqual(hist[-1]["decision"], "halt_entered")
        self.assertEqual(len(hist), 1)
        result2 = _upsert_today_history(hist,
                                        {"ts": "2026-07-23T10:00:00Z", "decision": "ok"})
        self.assertEqual(result2, "appended")
        self.assertEqual(len(hist), 2)


# ---------- Window helpers ---------------------------------------------
class TestRealizedDailySeries(unittest.TestCase):
    def test_days_zero_returns_empty(self):
        self.assertEqual(_realized_daily_series([], "BTCUSDm", days=0), [])

    def test_realized_series_chronological_ascending(self):
        trades = _build_realized("BTCUSDm",
                                 {d: 1.0 for d in range(5)})
        series = _realized_daily_series(trades, "BTCUSDm", days=5)
        self.assertEqual(len(series), 5)
        self.assertEqual(series[0], 1.0)
        self.assertEqual(series[-1], 1.0)

    def test_realized_series_zero_for_missing_symbol(self):
        trades = _build_realized("XAUUSDm", {0: 1.0})
        self.assertEqual(_realized_daily_series(trades, "BTCUSDm", days=5),
                         [0.0] * 5)

    def test_window_has_real_data(self):
        self.assertTrue(_window_has_real_data([0.0, 1.0, 0.0]))
        self.assertTrue(_window_has_real_data([-0.5, 0.0, 1.0]))
        self.assertFalse(_window_has_real_data([0.0, 0.0, 0.0]))
        self.assertFalse(_window_has_real_data([]))


# ---------- Persistence round-trip -------------------------------------
class TestPersistence(unittest.TestCase):
    def test_load_state_missing_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_state(path=Path(td) / "missing.json"), {})

    def test_save_state_writes_round_trippable_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "edge_monitor_state.json"
            doc = {
                "BTCUSDm": SymbolEdgeState(symbol="BTCUSDm",
                                           status=STATUS_HALT,
                                           halt_entered_count=1).to_dict(),
            }
            save_state(doc, path=p)
            self.assertTrue(p.exists(),
                            "save_state did not write to the requested path")
            reloaded = load_state(path=p)
            self.assertEqual(reloaded["BTCUSDm"]["status"], STATUS_HALT)
            self.assertEqual(reloaded["BTCUSDm"]["halt_entered_count"], 1)

    def test_save_state_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as td:
            nested = Path(td) / "nested" / "deeper" / "state.json"
            doc = {"BTCUSDm": SymbolEdgeState(
                symbol="BTCUSDm", status=STATUS_HALT).to_dict()}
            save_state(doc, path=nested)
            self.assertTrue(nested.exists())


# ---------- Documentation sanity ---------------------------------------
class TestDocsInvariants(unittest.TestCase):
    def test_module_docstring_documents_entry_conditions(self):
        text = (em.__doc__ or "")
        for token in ("E1", "E2", "E3", "E4", "E5"):
            self.assertIn(token, text, f"entry condition {token} missing from docstring")

    def test_module_docstring_documents_exit_conditions(self):
        text = (em.__doc__ or "")
        for token in ("X1", "X2", "X3"):
            self.assertIn(token, text, f"exit condition {token} missing from docstring")

    def test_module_docstring_documents_observe_only_invariant(self):
        text = (em.__doc__ or "")
        for token in ("config.yaml",
                      "learning_config_overrides.json",
                      "config_proposals.jsonl",
                      "learning_modifiers.json"):
            self.assertIn(token, text, f"no-write target {token} missing from docstring")

    def test_constants_match_spec(self):
        self.assertEqual(HALT_SIGMA_THRESHOLD, 2.0)
        self.assertEqual(HALT_DECAY_WINDOW_DAYS, 14)
        self.assertEqual(HALT_HALF_WINDOW_DAYS, 7)
        self.assertEqual(HALT_RECOVERY_REQUIRED_DAYS, 5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
