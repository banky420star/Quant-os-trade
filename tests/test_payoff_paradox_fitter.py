"""Tests for the Payoff Paradox self-tuning floor (2026-07-20).

Covers:
* core/utils.py payoff_paradox JSONL helpers (append, dedup, mtime cache)
* core/trade_tracker._build_payoff_paradox_row (row schema, BUY vs SELL maths)
* core/trade_tracker._log_payoff_paradox_audit_batch (call-site wrapper)
* core/trade_tracker._resolve_dotted_get (dotted-path resolve)
* scripts/backfill_payoff_paradox_audit.py (idempotency, dry-run, in-pass dedup)
* scripts/fit_payoff_paradox_floor.py (per-symbol fit, BTC promote, writeback)
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# core/utils.py appender
# ---------------------------------------------------------------------------

def test_append_payoff_paradox_audit_writes_one_row(tmp_state_dir, monkeypatch):
    """A single record without ts/symbol is rejected; a complete record lands."""
    from core import utils as core_utils
    monkeypatch.setattr(core_utils, "STATE_DIR", tmp_state_dir)
    # Drop the module-level mtime cache so reload sees the tmp dir.
    monkeypatch.setattr(core_utils, "_PAYOFF_PARADOX_AUDIT_CACHE", {})

    row = {
        "ts": "2026-07-20T12:00:00+00:00",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "entry": 3350.0,
        "exit": 3355.0,
        "sl": 3348.0,
        "r": 1.0,
        "would_have_been_locked_at": 3351.4,
        "projected_floor": 0.4,
        "result": "win",
        "pnl": 5.0,
        "ticket": "297123456",
    }
    written = core_utils.append_payoff_paradox_audit(row, filename="p.jsonl")
    assert written is not None
    assert written.exists()
    # The transaction_id is auto-stamped from (ts, ticket)
    raw = written.read_text(encoding="utf-8").strip()
    obj = json.loads(raw)
    assert obj["symbol"] == "XAUUSDm"
    assert obj["transaction_id"] == core_utils._payoff_paradox_tx(row)
    assert obj["archived_at"]


def test_append_payoff_paradox_audit_idempotent(tmp_state_dir, monkeypatch):
    """Re-appending the same (ts, ticket) is a no-op."""
    from core import utils as core_utils
    monkeypatch.setattr(core_utils, "STATE_DIR", tmp_state_dir)
    monkeypatch.setattr(core_utils, "_PAYOFF_PARADOX_AUDIT_CACHE", {})

    row = {
        "ts": "2026-07-20T12:00:00+00:00",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "entry": 3350.0,
        "exit": 3355.0,
        "sl": 3348.0,
        "r": 1.0,
        "would_have_been_locked_at": 3351.4,
        "projected_floor": 0.4,
        "result": "win",
        "pnl": 5.0,
        "ticket": "297123456",
    }
    p = tmp_state_dir / "p.jsonl"
    core_utils.append_payoff_paradox_audit(row, filename="p.jsonl")
    core_utils.append_payoff_paradox_audit(row, filename="p.jsonl")
    core_utils.append_payoff_paradox_audit(row, filename="p.jsonl")
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1


def test_append_payoff_paradox_audit_rejects_incomplete_records(tmp_state_dir, monkeypatch):
    from core import utils as core_utils
    monkeypatch.setattr(core_utils, "STATE_DIR", tmp_state_dir)
    monkeypatch.setattr(core_utils, "_PAYOFF_PARADOX_AUDIT_CACHE", {})
    p = tmp_state_dir / "p.jsonl"
    # missing 'ts' and missing 'symbol' → reject
    assert core_utils.append_payoff_paradox_audit({"x": 1}, filename="p.jsonl") is None
    # missing 'symbol'
    assert core_utils.append_payoff_paradox_audit({"ts": "t"}, filename="p.jsonl") is None
    # missing 'ts'
    assert core_utils.append_payoff_paradox_audit({"symbol": "X"}, filename="p.jsonl") is None
    assert not p.exists()


def test_append_payoff_paradox_audit_keeps_going_on_garbage(tmp_state_dir, monkeypatch):
    from core import utils as core_utils
    monkeypatch.setattr(core_utils, "STATE_DIR", tmp_state_dir)
    monkeypatch.setattr(core_utils, "_PAYOFF_PARADOX_AUDIT_CACHE", {})
    p = tmp_state_dir / "p.jsonl"
    p.write_text("not a json line\n", encoding="utf-8")
    row = {
        "ts": "2026-07-20T13:00:00+00:00",
        "symbol": "EURUSDm",
        "side": "SELL",
        "entry": 1.08500,
        "exit": 1.08450,
        "sl": 1.08540,
        "r": 1.0,
        "would_have_been_locked_at": 3349.6,  # typed wrong, should be None or float
        "projected_floor": 0.4,
        "result": "win",
        "pnl": 0.5,
        "ticket": "298888000",
    }
    written = core_utils.append_payoff_paradox_audit(row, filename="p.jsonl")
    assert written.exists()
    rows = core_utils.read_payoff_paradox_audit("p.jsonl")
    assert len(rows) == 1 and rows[0]["ticket"] == "298888000"


def test_read_payoff_paradox_audit_cached_refreshes_on_mtime(tmp_state_dir, monkeypatch):
    from core import utils as core_utils
    monkeypatch.setattr(core_utils, "STATE_DIR", tmp_state_dir)
    monkeypatch.setattr(core_utils, "_PAYOFF_PARADOX_AUDIT_CACHE", {})

    p = tmp_state_dir / "p.jsonl"
    core_utils.append_payoff_paradox_audit(
        make_minimal_row(ts="2026-07-20T10:00:00+00:00", ticket="t1"),
        filename="p.jsonl",
    )
    rows = core_utils.read_payoff_paradox_audit_cached("p.jsonl")
    assert len(rows) == 1
    # Cache hit returns same object even if file changed underneath.
    rows_again = core_utils.read_payoff_paradox_audit_cached("p.jsonl")
    assert rows_again is rows  # same list identity
    # Touch → mtime advance → cache invalidated.
    time_bump_mtime(p, +1.0)
    assert core_utils.read_payoff_paradox_audit_cached("p.jsonl") is not rows


def make_minimal_row(*, ts: str, ticket: str) -> dict[str, Any]:
    return {
        "ts": ts,
        "symbol": "XAUUSDm",
        "side": "BUY",
        "entry": 3350.0,
        "exit": 3355.0,
        "sl": 3348.0,
        "r": 1.0,
        "would_have_been_locked_at": None,
        "projected_floor": 0.4,
        "result": "win",
        "pnl": 5.0,
        "ticket": ticket,
    }


def time_bump_mtime(path: Path, delta: float) -> None:
    st = path.stat().st_mtime
    os.utime(path, (st + delta, st + delta))


@pytest.fixture
def tmp_state_dir(tmp_path):
    return tmp_path


# ---------------------------------------------------------------------------
# core/trade_tracker._build_payoff_paradox_row
# ---------------------------------------------------------------------------

def _trade_record(**overrides) -> dict[str, Any]:
    base = {
        "trade_id": "t-1",
        "position_id": "pid-1",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "entry": 3350.0,
        "exit": 3355.0,
        "sl": 3348.0,
        "tp1": 3355.0,
        "pnl": 5.0,
        "result": "win",
        "closed_at": "2026-07-20T12:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_build_payoff_paradox_row_buy_winner_math():
    from core.trade_tracker import _build_payoff_paradox_row
    trade = _trade_record(entry=3350.0, exit=3355.0, sl=3348.0, side="BUY")
    row = _build_payoff_paradox_row(trade, projected_floor=0.4)
    # risk_distance = 2.0; profit = 5.0 → r = 2.5
    assert row["r"] == round(5.0 / 2.0, 6)
    # would_have_been_locked_at = 3350 + 0.4*2.0 = 3350.8
    assert abs(row["would_have_been_locked_at"] - 3350.8) < 1e-6


def test_build_payoff_paradox_row_sell_winner_math():
    from core.trade_tracker import _build_payoff_paradox_row
    trade = _trade_record(entry=1.0850, exit=1.0845, sl=1.0854, side="SELL")
    row = _build_payoff_paradox_row(trade, projected_floor=0.5)
    # risk_distance = 0.0004; profit = 0.0005 → r = 1.25
    assert abs(row["r"] - 1.25) < 1e-3
    # would_have_been_locked_at = 1.0850 - 0.5*0.0004 = 1.08480
    assert abs(row["would_have_been_locked_at"] - 1.08480) < 1e-4


def test_build_payoff_paradox_row_loser_negative_r():
    from core.trade_tracker import _build_payoff_paradox_row
    trade = _trade_record(entry=3350.0, exit=3348.0, sl=3348.0, side="BUY", result="loss", pnl=-4.0)
    row = _build_payoff_paradox_row(trade, projected_floor=0.4)
    assert row["result"] == "loss"
    assert row["r"] < 0


def test_build_payoff_paradox_row_returns_none_for_bad_records():
    from core.trade_tracker import _build_payoff_paradox_row
    # missing entry
    assert _build_payoff_paradox_row({"symbol": "X", "side": "BUY", "exit": 1.0}, 0.4) is None
    # missing exit
    assert _build_payoff_paradox_row({"symbol": "X", "side": "BUY", "entry": 1.0}, 0.4) is None
    # bad side
    assert _build_payoff_paradox_row({"symbol": "X", "side": "LONG", "entry": 1.0, "exit": 2.0}, 0.4) is None
    # missing symbol
    assert _build_payoff_paradox_row({"side": "BUY", "entry": 1.0, "exit": 2.0}, 0.4) is None


def test_build_payoff_paradox_row_unpriced_sl_yields_none_r():
    """REVIEW FIX 3: r is None (not 0.0) when SL is unpriced so the fitter
    doesn't conflate unpriced losers with true zero-R winners.
    """
    from core.trade_tracker import _build_payoff_paradox_row
    trade = _trade_record(entry=3350.0, exit=3355.0, sl=None, side="BUY")
    row = _build_payoff_paradox_row(trade, projected_floor=0.4)
    assert row["r"] is None
    assert row["would_have_been_locked_at"] is None
    assert row["_has_priced_sl"] is False


def test_log_payoff_paradox_audit_batch_uses_config_projection(tmp_state_dir, monkeypatch):
    """When the config has payoff_paradox.projection_field, the row's
    projected_floor MUST come from that dotted path — and the per-trade
    hook skips rows when audit_enabled=False."""
    from core import trade_tracker
    from core import utils as core_utils
    monkeypatch.setattr(core_utils, "STATE_DIR", tmp_state_dir)
    monkeypatch.setattr(core_utils, "_PAYOFF_PARADOX_AUDIT_CACHE", {})

    cfg = {
        "trading": {
            "exits": {
                "min_r_multiple_win": 0.6,
                "payoff_paradox": {
                    "audit_enabled": True,
                    "projection_field": "trading.exits.min_r_multiple_win",
                },
            },
        },
    }
    trade = _trade_record(entry=3350.0, exit=3355.0, sl=3348.0, side="BUY", ticket="pid-77")
    n = trade_tracker._log_payoff_paradox_audit_batch([trade], cfg)
    assert n == 1
    rows = core_utils.read_payoff_paradox_audit("payoff_paradox_audit.jsonl")
    assert len(rows) == 1
    assert rows[0]["projected_floor"] == 0.6


def test_log_payoff_paradox_audit_batch_respects_audit_disabled(tmp_state_dir, monkeypatch):
    from core import trade_tracker
    from core import utils as core_utils
    monkeypatch.setattr(core_utils, "STATE_DIR", tmp_state_dir)
    monkeypatch.setattr(core_utils, "_PAYOFF_PARADOX_AUDIT_CACHE", {})
    cfg = {"trading": {"exits": {"payoff_paradox": {"audit_enabled": False}}}}
    assert trade_tracker._log_payoff_paradox_audit_batch([_trade_record()], cfg) == 0
    assert not (tmp_state_dir / "payoff_paradox_audit.jsonl").exists()


def test_resolve_dotted_get_navigates_nested():
    from core.trade_tracker import _resolve_dotted_get
    cfg = {"a": {"b": {"c": 0.7}}}
    assert _resolve_dotted_get(cfg, "a.b.c", 0.4) == 0.7
    assert _resolve_dotted_get(cfg, "a.b.missing", 0.4) == 0.4
    assert _resolve_dotted_get({}, "a.b", 0.4) == 0.4


def test_resolve_dotted_get_handles_string_floats():
    from core.trade_tracker import _resolve_dotted_get
    assert _resolve_dotted_get({"x": "0.6"}, "x", 0.4) == 0.6
    assert _resolve_dotted_get({"x": "abc"}, "x", 0.4) == 0.4


# ---------------------------------------------------------------------------
# scripts/backfill_payoff_paradox_audit.py
# ---------------------------------------------------------------------------

def test_backfill_happy_path(monkeypatch, tmp_path):
    """End-to-end: synthesize from a fake trade_log.json, append audit, count."""
    from core import utils as core_utils
    from scripts import backfill_payoff_paradox_audit as bmod

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(core_utils, "STATE_DIR", state_dir)
    monkeypatch.setattr(bmod, "STATE_DIR", state_dir)

    fake_config_path = state_dir / "config.yaml"
    fake_config_path.write_text(
        "trading:\n  exits:\n    min_r_multiple_win: 0.4\n",
        encoding="utf-8",
    )
    # Override PROJECT_ROOT so _safe_load_yaml_config reads our fake yaml.
    monkeypatch.setattr(bmod, "PROJECT_ROOT", state_dir)

    # Fake trade_log.json — note: SELL-loser math below is r = (entry-exit)/risk
    # = (140 - 140.5) / 0.2 = -2.5 (negative r). The earlier draft had
    # exit=139.5 which is a WIN for SELL (positive r), causing the test
    # to mis-rebel (r=2.5 vs asserted <0). Fixed by exit=140.5.
    fake_trades = [
        _trade_record(symbol="XAUUSDm", entry=3350.0, exit=3355.0, sl=3348.0, side="BUY",
                      position_id="pid-A", closed_at="2026-07-20T10:00:00+00:00"),
        _trade_record(symbol="XAUUSDm", entry=140.0, exit=140.5, sl=140.2, side="SELL",
                      position_id="pid-B", closed_at="2026-07-20T11:00:00+00:00", result="loss", pnl=-0.7),
        _trade_record(symbol="BTCUSDm", entry=65000.0, exit=65500.0, sl=64800.0, side="BUY",
                      position_id="pid-C", closed_at="2026-07-20T12:00:00+00:00"),
    ]
    (state_dir / "trade_log.json").write_text(
        json.dumps({"trades": fake_trades, "_last_mgmt_backfill_at": "t"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bmod, "_load_trade_log", lambda: (fake_trades, {"trades": fake_trades}))

    import sys as _sys
    _real_argv = _sys.argv
    try:
        _sys.argv = ["backfill_payoff_paradox_audit.py"]
        assert bmod.main() == 0
    finally:
        _sys.argv = _real_argv
    core_utils.reset_payoff_paradox_audit_caches_for_tests()

    audit = core_utils.read_payoff_paradox_audit("payoff_paradox_audit.jsonl")
    assert len(audit) == 3
    syms = sorted(r["symbol"] for r in audit)
    assert syms == ["BTCUSDm", "XAUUSDm", "XAUUSDm"]
    sell_xau = next(r for r in audit if r["side"] == "SELL" and r["symbol"] == "XAUUSDm")
    assert sell_xau["result"] == "loss"
    assert sell_xau["r"] is not None and sell_xau["r"] < 0
    assert sell_xau["_has_priced_sl"] is True


def test_backfill_idempotent(monkeypatch, tmp_path):
    from core import utils as core_utils
    from scripts import backfill_payoff_paradox_audit as bmod

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(core_utils, "STATE_DIR", state_dir)
    monkeypatch.setattr(bmod, "STATE_DIR", state_dir)

    fake = [_trade_record(position_id="pid-1", closed_at="2026-07-20T10:00:00+00:00")]
    (state_dir / "trade_log.json").write_text(json.dumps({"trades": fake}), encoding="utf-8")
    monkeypatch.setattr(bmod, "_load_trade_log", lambda: (fake, {"trades": fake}))

    import sys as _sys
    for _ in range(3):
        _real = _sys.argv
        _sys.argv = ["backfill_payoff_paradox_audit.py"]
        try:
            assert bmod.main() == 0
        finally:
            _sys.argv = _real

    rows = core_utils.read_payoff_paradox_audit("payoff_paradox_audit.jsonl")
    assert len(rows) == 1


def test_backfill_dry_run_does_not_mutate(monkeypatch, tmp_path):
    from core import utils as core_utils
    from scripts import backfill_payoff_paradox_audit as bmod

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(core_utils, "STATE_DIR", state_dir)
    monkeypatch.setattr(bmod, "STATE_DIR", state_dir)
    fake = [_trade_record(position_id="pid-X", closed_at="2026-07-20T10:00:00+00:00")]
    (state_dir / "trade_log.json").write_text(json.dumps({"trades": fake}), encoding="utf-8")
    monkeypatch.setattr(bmod, "_load_trade_log", lambda: (fake, {"trades": fake}))

    import sys as _sys
    _real = _sys.argv
    _sys.argv = ["backfill_payoff_paradox_audit.py", "--dry-run"]
    try:
        assert bmod.main() == 0
    finally:
        _sys.argv = _real
    assert not (state_dir / "payoff_paradox_audit.jsonl").exists()


# ---------------------------------------------------------------------------
# scripts/fit_payoff_paradox_floor.py
# ---------------------------------------------------------------------------

def test_floor_for_median_r_three_buckets():
    from scripts.fit_payoff_paradox_floor import floor_for_median_r
    assert floor_for_median_r(0.10) == 0.4
    assert floor_for_median_r(0.30) == 0.4
    assert floor_for_median_r(0.45) == 0.4
    assert floor_for_median_r(0.46) == 0.5
    assert floor_for_median_r(0.55) == 0.5
    assert floor_for_median_r(0.65) == 0.5
    assert floor_for_median_r(0.66) == 0.6
    assert floor_for_median_r(0.90) == 0.6


def test_floor_for_median_r_zero_or_negative_falls_back():
    from scripts.fit_payoff_paradox_floor import floor_for_median_r
    assert floor_for_median_r(0.0) == 0.4
    assert floor_for_median_r(-0.5) == 0.4


def _build_audit_rows(symbol: str, n: int, r_values: list[float]) -> list[dict[str, Any]]:
    rows = []
    for i, r in enumerate(r_values):
        side = "BUY" if r >= 0 else "SELL"
        rows.append({
            "ts": f"2026-07-20T{10 + i:02d}:00:00+00:00",
            "symbol": symbol,
            "side": side,
            "entry": 100.0,
            "exit": 100.0 + r if side == "BUY" else 100.0 - r,
            "sl": 99.0,
            "r": r,
            "would_have_been_locked_at": None,
            "projected_floor": 0.4,
            "result": "win" if r > 0 else "loss",
            "pnl": r,
            "ticket": f"{symbol}-{i}",
        })
    return rows


def test_fit_per_symbol_uses_minimum_thresholds():
    from scripts.fit_payoff_paradox_floor import fit_per_symbol
    # 12 rows, 5 winners, median winner r ≈ 0.55
    winners = [0.30, 0.50, 0.55, 0.60, 0.80]
    losers = [-1.0] * 7
    rows = _build_audit_rows("XAUUSDm", n=12, r_values=winners + losers)
    out = fit_per_symbol(rows, current_floor=0.4, min_per_symbol_winners=10, min_median_winner_r=0.15)
    assert len(out) == 1
    sym = out[0]
    assert sym["symbol"] == "XAUUSDm"
    assert sym["n_winners"] == 5
    # Less than min_per_symbol_winners=10 → stays at floor 0.4
    assert sym["recommended_floor"] == 0.4
    assert "insufficient_winners" in sym["reason"]


def test_fit_per_symbol_elevates_on_strong_median():
    from scripts.fit_payoff_paradox_floor import fit_per_symbol
    # 14 winners, median winner r = 0.70 → floor=0.6
    winners = [0.40, 0.50, 0.55, 0.65, 0.70, 0.70, 0.75, 0.80, 0.90, 1.00, 1.10, 1.20, 1.30, 1.50]
    losers = [-1.0] * 6
    rows = _build_audit_rows("XAUUSDm", n=20, r_values=winners + losers)
    out = fit_per_symbol(rows, current_floor=0.4, min_per_symbol_winners=10, min_median_winner_r=0.15)
    sym = out[0]
    assert sym["recommended_floor"] == 0.6


def test_fit_btc_promote_eligible_with_stable_high_median():
    from scripts.fit_payoff_paradox_floor import fit_btc_promote
    # 30 BTC winners, tight cluster around 0.55–0.70
    winners = [0.50 + 0.005 * i for i in range(30)]  # 0.50 .. 0.645
    losers = [-1.0] * 5
    rows = _build_audit_rows("BTCUSDm", n=35, r_values=winners + losers)
    verdict = fit_btc_promote(
        rows, current_floor=0.4,
        btc_cfg={"symbol": "BTCUSDm", "min_n": 30, "median_threshold": 0.5,
                 "promote_floor": 0.5, "enabled": True},
    )
    assert verdict["eligible"] is True
    assert verdict["recommend_promote"] is True
    assert verdict["median_winner_r_last_n"] > 0.5


def test_fit_btc_promote_not_eligible_too_few_winners():
    from scripts.fit_payoff_paradox_floor import fit_btc_promote
    winners = [0.55 + 0.005 * i for i in range(15)]  # only 15 winners
    losers = [-1.0] * 30
    rows = _build_audit_rows("BTCUSDm", n=45, r_values=winners + losers)
    verdict = fit_btc_promote(
        rows, current_floor=0.4,
        btc_cfg={"symbol": "BTCUSDm", "min_n": 30, "median_threshold": 0.5,
                 "promote_floor": 0.5, "enabled": True},
    )
    # n_last_n_winners is the BTC winners filtered to last btc_min_n; with only
    # 15 winners total, len(last 30) = 15. Eligibility requires >= 30, so no.
    assert verdict["eligible"] is False


def test_fit_btc_promote_high_median_but_high_variance_unstable():
    """IQR/median >= 0.5 fails the stability check."""
    from scripts.fit_payoff_paradox_floor import fit_btc_promote
    # Highly bimodal winners — some 0.3, some 1.8 → wide IQR, ratio >= 0.5
    winners = [0.30] * 15 + [1.80] * 15
    losers = [-1.0] * 5
    rows = _build_audit_rows("BTCUSDm", n=35, r_values=winners + losers)
    verdict = fit_btc_promote(
        rows, current_floor=0.4,
        btc_cfg={"symbol": "BTCUSDm", "min_n": 30, "median_threshold": 0.5,
                 "promote_floor": 0.5, "enabled": True},
    )
    assert verdict["eligible"] is False
    assert verdict["iqr_over_median"] is not None and verdict["iqr_over_median"] >= 0.5


def test_infer_global_floor_snap_to_nearest_candidate():
    from scripts.fit_payoff_paradox_floor import infer_global_floor
    per = [
        {"symbol": "X", "recommended_floor": 0.6, "current_floor": 0.4},
        {"symbol": "Y", "recommended_floor": 0.5, "current_floor": 0.4},
    ]
    floor, _reason = infer_global_floor(per, btc_verdict={"eligible": False,
                                                          "recommend_promote": False},
                                         current_floor=0.4)
    assert floor == 0.5


def test_infer_global_floor_btc_promote_overrides():
    from scripts.fit_payoff_paradox_floor import infer_global_floor
    per = [{"symbol": "X", "recommended_floor": 0.4, "current_floor": 0.4}]
    floor, reason = infer_global_floor(
        per,
        btc_verdict={"eligible": True, "recommend_promote": True, "promote_floor": 0.5,
                     "median_winner_r_last_n": 0.7, "median_threshold": 0.5},
        current_floor=0.4,
    )
    assert floor == 0.5
    assert "BTC_AUTO_PROMOTE" in reason


def test_infer_global_floor_never_steps_down():
    from scripts.fit_payoff_paradox_floor import infer_global_floor
    per = [
        {"symbol": "A", "recommended_floor": 0.4, "current_floor": 0.4},
        {"symbol": "B", "recommended_floor": 0.4, "current_floor": 0.4},
    ]
    floor, _ = infer_global_floor(per, {"eligible": False, "recommend_promote": False},
                                  current_floor=0.6)
    assert floor == 0.6
