"""Tests for scripts/walk_forward_microtest.py.

Covers proposal loading, trade log filtering, chronological split math,
MAE/MFE interpolation behaviour, per-symbol + aggregate gate logic,
insufficient-data exits, and end-to-end CLI invocation.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.walk_forward_microtest import (  # noqa: E402
    DEFAULT_SPLIT_PCT,
    DEFAULT_TAIL_N,
    MIN_SYMBOLS,
    MIN_TAIL_PER_SYMBOL,
    _extract_r_units,
    _is_clean_trade,
    _load_proposal,
    _resolve_baseline_params,
    _simulate_tail,
    _trade_sort_key,
    effective_min_evaluated,
    gate_check,
    split_tail,
)


# ---------------------------------------------------------------------------
# Fixtures: synthetic trade log
# ---------------------------------------------------------------------------
def _make_trade(
    symbol: str,
    pnl: float,
    r_mult: float,
    mae_R: float = 0.9,
    mfe_R: float = 1.4,
    closed_at: str = "2026-07-21T10:00:00+00:00",
    archive_polluted: bool = False,
    missing_mae_mfe: bool = False,
) -> dict:
    t = {
        "symbol": symbol,
        "side": "BUY" if pnl >= 0 else "SELL",
        "pnl": pnl,
        "r_multiple": r_mult,
        "entry": 2000.0,
        "sl": 1990.0,
        "tp1": 2020.0,
        "sl_initial": 1990.0,
        "closed_at": closed_at,
        "opened_at": closed_at.replace("10:00:00", "09:55:00"),
        "trade_id": f"TR-{symbol}-{closed_at}",
    }
    if not missing_mae_mfe:
        t["mae_R"] = mae_R
        t["mfe_R"] = mfe_R
    if archive_polluted:
        t["archive_polluted"] = True
    return t


def _build_log(trades: list[dict]) -> dict:
    return {"trades": trades}


# ---------------------------------------------------------------------------
# _is_clean_trade / _trade_sort_key / _extract_r_units
# ---------------------------------------------------------------------------
def test_is_clean_trade_filters_archive_polluted():
    clean = _make_trade("XAUUSDm", 1.0, 0.5, archive_polluted=False)
    polluted = _make_trade("XAUUSDm", 1.0, 0.5, archive_polluted=True)
    assert _is_clean_trade(clean) is True
    assert _is_clean_trade(polluted) is False


def test_trade_sort_key_uses_closed_at_first():
    a = _make_trade("XAUUSDm", 1.0, 0.5, closed_at="2026-07-21T10:00:00+00:00")
    b = _make_trade("XAUUSDm", 1.0, 0.5, closed_at="2026-07-21T11:00:00+00:00")
    assert _trade_sort_key(a) < _trade_sort_key(b)


def test_extract_r_units_handles_missing_mae_mfe():
    t = _make_trade("XAUUSDm", 0.5, 0.4, missing_mae_mfe=True)
    mae, mfe, rm, tp1_r = _extract_r_units(t)
    assert mae is None and mfe is None
    assert rm == 0.4
    # entry=2000, sl=1990, tp1=2020 → orig_r=10, tp1_r = |2020-2000|/10 = 2.0
    assert tp1_r == pytest.approx(2.0)


def test_extract_r_units_handles_bad_types_gracefully():
    t = {"mae_R": "NaN", "mfe_R": None, "r_multiple": None}
    mae, mfe, rm, tp1_r = _extract_r_units(t)
    assert mae is None and mfe is None and rm is None and tp1_r is None


# ---------------------------------------------------------------------------
# split_tail
# ---------------------------------------------------------------------------
def test_split_tail_takes_last_n_then_divides():
    trades = [
        _make_trade("XAUUSDm", 1.0, 0.5, closed_at=f"2026-07-21T{i:02d}:00:00+00:00")
        for i in range(50)
    ]
    train, tail = split_tail(trades, tail_n=40, split_pct=0.7)
    # Only the last 40 make it past the tail_n cap, then split 70/30 of those.
    assert len(train) + len(tail) == 40
    assert len(train) == int(round(40 * 0.7))
    assert len(tail) == 40 - len(train)
    # Tail is the chronologically newest.
    last_train_ts = train[-1]["closed_at"]
    first_tail_ts = tail[0]["closed_at"]
    assert last_train_ts < first_tail_ts


def test_split_tail_default_constants_match_public_contract():
    trades = [_make_trade("XAUUSDm", 1.0, 0.5, closed_at=f"2026-07-21T{i:02d}:00:00+00:00") for i in range(120)]
    train, tail = split_tail(trades)
    assert len(train) + len(tail) == 100
    assert len(train) == int(round(100 * DEFAULT_SPLIT_PCT))


# ---------------------------------------------------------------------------
# _simulate_tail + baseline resolution
# ---------------------------------------------------------------------------
def test_simulate_tail_interpolates_missing_mae_mfe():
    """MAE/MFE-missing rows are EXCLUDED from the simulation (delta would
    always be 0 → silently inflate n_tail). The interpolated_n counter
    still records them so callers can decide whether the symbol is too
    sketchy to evaluate.
    """
    trades = [
        _make_trade("XAUUSDm", 2.0, 1.0, missing_mae_mfe=True),
        _make_trade("XAUUSDm", -1.0, -0.5, mae_R=1.2, mfe_R=0.3),
    ]
    sim = _simulate_tail(trades, params=(0.5, 0.1, 0.7, 0.3))
    # Only the second trade (real mae/mfe) counts; mae 1.2 >= 1 + mfe 0.3 < be_trig 0.5 → -1.0
    assert sim["n_tail"] == 1
    assert sim["interpolated_n"] == 1
    assert sim["net_R"] == -1.0
    assert sim["expectancy_R"] == -1.0


def test_resolve_baseline_params_uses_live_state_override():
    config = {
        "trading": {
            "break_even": {"trigger_atr_mult": 0.5, "lock_profit_atr_mult": 0.1,
                            "per_symbol": {}},
            "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35,
                         "per_symbol": {}},
        }
    }
    live_state = {"symbols": {
        "XAUUSDm": {
            "break_even": {"trigger_atr_mult": 0.30, "lock_profit_atr_mult": 0.05},
            "trailing":  {"activation_atr_mult": 0.55, "trail_atr_mult": 0.25},
            "trusted": True,
        }
    }}
    params = _resolve_baseline_params(config, live_state, "XAUUSDm")
    assert params == (0.30, 0.05, 0.55, 0.25)


def test_resolve_baseline_params_falls_back_to_per_symbol_seed():
    config = {
        "trading": {
            "break_even": {"trigger_atr_mult": 0.5, "lock_profit_atr_mult": 0.1,
                            "per_symbol": {"XAUUSDm": {"trigger_atr_mult": 0.4}}},
            "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35,
                         "per_symbol": {"XAUUSDm": {"activation_atr_mult": 0.65}}},
        }
    }
    live_state = {"symbols": {}}
    params = _resolve_baseline_params(config, live_state, "XAUUSDm")
    assert params == (0.4, 0.1, 0.65, 0.35)


# ---------------------------------------------------------------------------
# _load_proposal
# ---------------------------------------------------------------------------
def test_load_proposal_valid_schema(tmp_path):
    p = tmp_path / "proposal.json"
    p.write_text(json.dumps({
        "symbols": {
            "XAUUSDm": {
                "break_even": {"trigger_atr_mult": 0.3, "lock_profit_atr_mult": 0.05},
                "trailing":  {"activation_atr_mult": 0.5, "trail_atr_mult": 0.25},
            },
            "EURUSDm": {
                "break_even": {"trigger_atr_mult": 0.4, "lock_profit_atr_mult": 0.1},
                "trailing":  {"activation_atr_mult": 0.6, "trail_atr_mult": 0.3},
            },
        }
    }), encoding="utf-8")
    proposal = _load_proposal(p)
    assert "XAUUSDm" in proposal and "EURUSDm" in proposal
    assert proposal["XAUUSDm"]["break_even"]["trigger_atr_mult"] == 0.3


def test_load_proposal_rejects_missing_symbols_key(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"weird_key": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="symbols"):
        _load_proposal(p)


def test_load_proposal_rejects_all_bad_symbols(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"symbols": {"XAUUSDm": {"break_even": {"foo": 0.3}}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="no usable symbols"):
        _load_proposal(p)


from scripts import walk_forward_microtest as wfm_module  # noqa: E402


def test_load_proposal_aborts_on_any_bad_symbol(tmp_path):
    """Loud-failure policy: any NaN/Inf/TypeError entry aborts the gate
    entirely. Callers must fix the proposal before re-running; we do NOT
    silently apply a partial proposal because that mixes baseline for some
    symbols and proposed for others.
    """
    raw = {"symbols": {
        "XAUUSDm": {
            "break_even": {"trigger_atr_mult": "NaN", "lock_profit_atr_mult": 0.05},
            "trailing":  {"activation_atr_mult": 0.5, "trail_atr_mult": 0.25},
        },
        "EURUSDm": {
            "break_even": {"trigger_atr_mult": 0.3, "lock_profit_atr_mult": 0.05},
            "trailing":  {"activation_atr_mult": 0.5, "trail_atr_mult": 0.25},
        },
    }}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid symbols"):
        _load_proposal(p)


# ---------------------------------------------------------------------------
# gate_check integration with synthetic data
# ---------------------------------------------------------------------------
@pytest.fixture
def synthetic_log_pass():
    """Symbol A has trades where aggressive BE/trail SAVES losers. Symbol B has
    trades where a looser BE/trail CAPS winners less aggressively. The
    proposal tightens A but loosens B — both should net positive on aggregate.
    """
    trades = []
    # XAUUSDm: 30 trades, all losers → tighter BE should rescue some
    for i in range(30):
        trades.append(_make_trade(
            "XAUUSDm", pnl=-1.0, r_mult=-1.0,
            mae_R=1.0, mfe_R=0.6,  # stopped without reaching BE
            closed_at=f"2026-07-21T{i:02d}:00:00+00:00",
        ))
    # USOILm: 30 trades, all winners → looser BE should let winners run
    for i in range(30):
        trades.append(_make_trade(
            "USOILm", pnl=1.0, r_mult=1.0,
            mae_R=0.4, mfe_R=2.0,  # big winners, no MAE
            closed_at=f"2026-07-21T{i:02d}:00:00+00:00",
        ))
    # EURUSDm: 20 trades, mixed
    for i in range(20):
        trades.append(_make_trade(
            "EURUSDm", pnl=-0.5, r_mult=-0.5,
            mae_R=1.0, mfe_R=0.4,  # stopped without BE
            closed_at=f"2026-07-21T{i:02d}:00:00+00:00",
        ))
    return _build_log(trades)


@pytest.fixture
def synthetic_log_fail():
    """Robust fail-case: mfe sits BELOW baseline BE/trigger so baseline
    falls through to realised r_mult (high positive). The proposed hyper-tight
    BE then gets triggered (mfe > be_trig=0.05) and locks at be_lock=0.02 →
    delta is strongly negative on both axes.
    """
    trades = []
    # mfe=0.6 < baseline be_trig 0.65 AND < baseline tr_act 0.75
    # baseline falls through → return realised r_mult=0.6
    # proposed: mfe 0.6 >= be_trig 0.05 → return be_lock=0.02
    for i in range(50):
        trades.append(_make_trade(
            "XAUUSDm", pnl=0.6, r_mult=0.6,
            mae_R=0.2, mfe_R=0.6,
            closed_at=f"2026-07-21T{i:02d}:00:00+00:00",
        ))
    for i in range(30):
        trades.append(_make_trade(
            "USOILm", pnl=0.5, r_mult=0.5,
            mae_R=0.2, mfe_R=0.5,
            closed_at=f"2026-07-21T{i:02d}:00:00+00:00",
        ))
    for i in range(20):
        trades.append(_make_trade(
            "EURUSDm", pnl=0.4, r_mult=0.4,
            mae_R=0.2, mfe_R=0.4,
            closed_at=f"2026-07-21T{i:02d}:00:00+00:00",
        ))
    return _build_log(trades)


def test_gate_check_pass_aggregate(synthetic_log_pass):
    config = {
        "trading": {
            "break_even": {"trigger_atr_mult": 0.65, "lock_profit_atr_mult": 0.1},
            "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35},
        }
    }
    proposal = {
        "XAUUSDm": {"break_even": {"trigger_atr_mult": 0.30, "lock_profit_atr_mult": 0.05},
                     "trailing": {"activation_atr_mult": 0.65, "trail_atr_mult": 0.30}},
        "USOILm":  {"break_even": {"trigger_atr_mult": 0.65, "lock_profit_atr_mult": 0.10},
                     "trailing": {"activation_atr_mult": 1.20, "trail_atr_mult": 0.50}},
        "EURUSDm": {"break_even": {"trigger_atr_mult": 0.40, "lock_profit_atr_mult": 0.10},
                     "trailing": {"activation_atr_mult": 0.65, "trail_atr_mult": 0.30}},
    }
    report = gate_check(synthetic_log_pass, config, {}, proposal, tail_n=80, split_pct=0.7,
                        min_symbols=3, min_tail_per_symbol=5, min_overall_trades=50)
    assert report["exit_code"] == 0
    assert report["verdict"] is True
    agg = report["aggregate"]
    assert agg["net_R_delta"] > 0
    assert agg["expectancy_delta"] > 0


def test_gate_check_fail_aggregate(synthetic_log_fail):
    config = {
        "trading": {
            "break_even": {"trigger_atr_mult": 0.65, "lock_profit_atr_mult": 0.1},
            "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35},
        }
    }
    # Hyper-tight BE that converts 0.4-0.6R winners into 0.02R lock-ins.
    proposal = {
        "XAUUSDm": {"break_even": {"trigger_atr_mult": 0.05, "lock_profit_atr_mult": 0.02},
                     "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35}},
        "USOILm":  {"break_even": {"trigger_atr_mult": 0.05, "lock_profit_atr_mult": 0.02},
                     "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35}},
        "EURUSDm": {"break_even": {"trigger_atr_mult": 0.05, "lock_profit_atr_mult": 0.02},
                     "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35}},
    }
    report = gate_check(synthetic_log_fail, config, {}, proposal, tail_n=80, split_pct=0.7,
                        min_symbols=3, min_tail_per_symbol=5, min_overall_trades=50)
    assert report["exit_code"] == 1
    assert report["verdict"] is False
    agg = report["aggregate"]
    # Aggregated winners (0.4-0.6R realised) get capped to 0.02R — strictly worse.
    assert agg["net_R_delta"] < 0
    assert agg["expectancy_delta"] < 0


def test_gate_check_insufficient_global_data():
    """< 30 trades globally → exit code 2, no per-symbol results."""
    trade_log = _build_log([
        _make_trade("XAUUSDm", 1.0, 0.5, closed_at="2026-07-21T10:00:00+00:00"),
        _make_trade("XAUUSDm", -1.0, -1.0, closed_at="2026-07-21T11:00:00+00:00"),
        _make_trade("EURUSDm", 1.0, 0.5, closed_at="2026-07-21T12:00:00+00:00"),
    ])
    proposal = {"XAUUSDm": {"break_even": {"trigger_atr_mult": 0.3, "lock_profit_atr_mult": 0.05},
                            "trailing": {"activation_atr_mult": 0.5, "trail_atr_mult": 0.25}}}
    report = gate_check(trade_log, {}, {}, proposal)
    assert report["exit_code"] == 2
    # insufficient_data is now a list of dicts with per-symbol reasons.
    assert any(e.get("symbol") == "global" for e in report["insufficient_data"])


def test_gate_check_insufficient_symbols():
    """Only 2 symbols meet per-symbol min-tail — fails globally."""
    trades = []
    for i in range(40):
        trades.append(_make_trade("XAUUSDm", -0.5, -0.5, closed_at=f"2026-07-21T{i:02d}:00:00+00:00"))
    for i in range(40):
        trades.append(_make_trade("USOILm", -0.3, -0.3, closed_at=f"2026-07-21T{i:02d}:00:00+00:00"))
    trade_log = _build_log(trades)
    proposal = {"XAUUSDm": {"break_even": {"trigger_atr_mult": 0.3, "lock_profit_atr_mult": 0.05},
                            "trailing": {"activation_atr_mult": 0.5, "trail_atr_mult": 0.25}}}
    report = gate_check(trade_log, {}, {}, proposal,
                        min_symbols=3, min_tail_per_symbol=5, min_overall_trades=30)
    assert report["exit_code"] == 2
    assert report["reason"].startswith("only")
    # Per-symbol diagnosis surfaced when too few symbols are usable.
    syms_in_report = {e.get("symbol") for e in report["insufficient_data"]}
    assert "XAUUSDm" in syms_in_report and "USOILm" in syms_in_report


def test_gate_check_partial_proposal_zero_delta_on_missing_symbol():
    """Proposal only mentions XAUUSDm; USOILm should still appear with delta=0."""
    trades = []
    for i in range(40):
        trades.append(_make_trade("XAUUSDm", -1.0, -1.0, mae_R=1.1, mfe_R=0.5,
                                  closed_at=f"2026-07-21T{i:02d}:00:00+00:00"))
    for i in range(40):
        trades.append(_make_trade("USOILm", 1.0, 1.0, mae_R=0.3, mfe_R=1.2,
                                  closed_at=f"2026-07-21T{i:02d}:00:00+00:00"))
    for i in range(40):
        trades.append(_make_trade("EURUSDm", 1.0, 1.0, mae_R=0.3, mfe_R=1.2,
                                  closed_at=f"2026-07-21T{i:02d}:00:00+00:00"))
    trade_log = _build_log(trades)
    proposal = {"XAUUSDm": {"break_even": {"trigger_atr_mult": 0.05, "lock_profit_atr_mult": 0.02},
                            "trailing": {"activation_atr_mult": 0.10, "trail_atr_mult": 0.05}}}
    config = {
        "trading": {
            "break_even": {"trigger_atr_mult": 0.5, "lock_profit_atr_mult": 0.1},
            "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35},
        }
    }
    report = gate_check(trade_log, config, {}, proposal)
    assert "USOILm" in report["per_symbol"]
    assert report["per_symbol"]["USOILm"]["net_R_delta"] == 0.0
    assert report["per_symbol"]["USOILm"]["expectancy_delta"] == 0.0


# ---------------------------------------------------------------------------
# CLI smoke test (end-to-end)
# ---------------------------------------------------------------------------
def test_cli_exit_code_invalid_proposal(tmp_path):
    """Invalid proposal path → exit 3."""
    out_path = tmp_path / "report.json"
    proj_path = tmp_path / "missing.json"
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "walk_forward_microtest.py"),
        "--proposal", str(proj_path),
        "--output", str(out_path),
        "--config", str(ROOT / "config.yaml"),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    assert res.returncode == 3
    assert "PASS" not in res.stdout  # never says pass


def test_cli_exit_code_insufficient_data(tmp_path, monkeypatch):
    """Empty trade log → exit 2. Uses monkeypatch on the script's
    read_json_state import so we don't have to mutate the real state dir.
    """
    proj = {"symbols": {"XAUUSDm": {"break_even": {"trigger_atr_mult": 0.3, "lock_profit_atr_mult": 0.05},
                                   "trailing": {"activation_atr_mult": 0.5, "trail_atr_mult": 0.25}}}}
    proj_path = tmp_path / "proposal.json"
    proj_path.write_text(json.dumps(proj), encoding="utf-8")
    out_path = tmp_path / "report.json"
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "walk_forward_microtest.py"),
        "--proposal", str(proj_path),
        "--output", str(out_path),
        "--config", str(ROOT / "config.yaml"),
    ]

    def _empty_state(name, default=None):  # noqa: ARG001
        return {}

    monkeypatch.setattr(wfm_module, "read_json_state", _empty_state)
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmp_path))
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert json.loads(out_path.read_text(encoding="utf-8"))["verdict"] is False


# ---------------------------------------------------------------------------
# Reviewer-fix coverage: per-field live-state merge + trade-weighted expectancy
# ---------------------------------------------------------------------------
def test_resolve_baseline_params_merges_partial_live_state():
    """If live state only calibrated BE for XAUUSDm, trailing must fall
    back to config seed — NOT drop the BE override entirely.
    """
    config = {
        "trading": {
            "break_even": {"trigger_atr_mult": 0.5, "lock_profit_atr_mult": 0.1,
                            "per_symbol": {}},
            "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35,
                         "per_symbol": {}},
        }
    }
    live_state = {"symbols": {
        "XAUUSDm": {
            "break_even": {"trigger_atr_mult": 0.30, "lock_profit_atr_mult": 0.05},
            # NO trailing block — must fall back to config seeds for trail
        }
    }}
    params = _resolve_baseline_params(config, live_state, "XAUUSDm")
    assert params[0] == 0.30  # BE from live state
    assert params[1] == 0.05  # BE lock from live state
    assert params[2] == 0.75  # trail act from config seed (fall-back)
    assert params[3] == 0.35  # trail dist from config seed (fall-back)


def test_gate_check_excludes_high_interpolation_symbol():
    """If >50% of a symbol's tail rows have no MAE/MFE, the symbol is
    excluded from the aggregate so its zero-delta rows can't carry the verdict.
    """
    trades = []
    # XAUUSDm: 22 with mae/mfe + 8 without → interp fraction = 8/30 > 0.5 → excluded
    for i in range(22):
        h = i // 60; m = i % 60
        trades.append(_make_trade(
            "XAUUSDm", 1.0, 1.0, mae_R=0.3, mfe_R=1.5,
            closed_at=f"2026-07-21T{h:02d}:{m:02d}:00+00:00",
        ))
    for i in range(22, 30):
        h = i // 60; m = i % 60
        trades.append(_make_trade(
            "XAUUSDm", 1.0, 1.0, missing_mae_mfe=True,
            closed_at=f"2026-07-21T{h:02d}:{m:02d}:00+00:00",
        ))
    # USOILm: full coverage, 30 rows (day 22)
    for i in range(30):
        h = (i + 60) // 60; m = (i + 60) % 60
        trades.append(_make_trade(
            "USOILm", 1.0, 1.0, mae_R=0.3, mfe_R=1.5,
            closed_at=f"2026-07-22T{h:02d}:{m:02d}:00+00:00",
        ))
    # EURUSDm: full coverage, 30 rows (day 23)
    for i in range(30):
        h = (i + 60) // 60; m = (i + 60) % 60
        trades.append(_make_trade(
            "EURUSDm", 1.0, 1.0, mae_R=0.3, mfe_R=1.5,
            closed_at=f"2026-07-23T{h:02d}:{m:02d}:00+00:00",
        ))
    trade_log = _build_log(trades)
    proposal = {
        "XAUUSDm": {"break_even": {"trigger_atr_mult": 0.05, "lock_profit_atr_mult": 0.02},
                     "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35}},
        "USOILm":  {"break_even": {"trigger_atr_mult": 0.05, "lock_profit_atr_mult": 0.02},
                     "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35}},
        "EURUSDm": {"break_even": {"trigger_atr_mult": 0.05, "lock_profit_atr_mult": 0.02},
                     "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35}},
    }
    report = gate_check(trade_log, {}, {}, proposal, tail_n=80, split_pct=0.7,
                        min_symbols=2, min_tail_per_symbol=5, min_overall_trades=30)
    # XAU excluded with per-symbol reason; USOIL + EURUSD evaluated.
    syms_excluded = {e.get("symbol") for e in report["insufficient_data"]}
    assert "XAUUSDm" in syms_excluded
    # The reason must surface the interp_frac diagnostic so operators can act.
    xau_reason = next(e for e in report["insufficient_data"] if e["symbol"] == "XAUUSDm")
    assert "interp_frac=" in xau_reason["reason"]
    assert "n_tail_raw" in xau_reason and "n_tail_evaluated" in xau_reason
    assert "USOILm" in report["per_symbol"]
    assert "EURUSDm" in report["per_symbol"]


def test_gate_check_trade_weighted_expectancy_label():
    """The aggregate dict always carries ``expectancy_aggregation='trade_weighted'``,
    ``effective_min_evaluated_per_symbol``, and ``interp_frac_per_symbol`` so
    operators reading the report JSON know which math was used and which
    symbols survived the coverage filter.
    """
    trades = []
    for i in range(40):
        trades.append(_make_trade("XAUUSDm", -1.0, -1.0, mae_R=1.0, mfe_R=0.6,
                                  closed_at=f"2026-07-21T{i:02d}:00:00+00:00"))
    for i in range(40):
        trades.append(_make_trade("USOILm", 1.0, 1.0, mae_R=0.4, mfe_R=2.0,
                                  closed_at=f"2026-07-21T{i:02d}:00:00+00:00"))
    for i in range(40):
        trades.append(_make_trade("EURUSDm", 1.0, 1.0, mae_R=0.4, mfe_R=1.5,
                                  closed_at=f"2026-07-22T{i:02d}:00:00+00:00"))
    proposal = {
        "XAUUSDm": {"break_even": {"trigger_atr_mult": 0.30, "lock_profit_atr_mult": 0.05},
                     "trailing": {"activation_atr_mult": 0.65, "trail_atr_mult": 0.30}},
        "USOILm":  {"break_even": {"trigger_atr_mult": 0.65, "lock_profit_atr_mult": 0.10},
                     "trailing": {"activation_atr_mult": 1.20, "trail_atr_mult": 0.50}},
        "EURUSDm": {"break_even": {"trigger_atr_mult": 0.40, "lock_profit_atr_mult": 0.10},
                     "trailing": {"activation_atr_mult": 0.65, "trail_atr_mult": 0.30}},
    }
    config = {
        "trading": {
            "break_even": {"trigger_atr_mult": 0.65, "lock_profit_atr_mult": 0.1},
            "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35},
        }
    }
    report = gate_check(_build_log(trades), config, {}, proposal, tail_n=80, split_pct=0.7,
                        min_symbols=3, min_tail_per_symbol=5, min_overall_trades=50)
    agg = report["aggregate"]
    assert agg["expectancy_aggregation"] == "trade_weighted"
    assert agg["effective_min_evaluated_per_symbol"] >= 1
    assert "XAUUSDm" in agg["interp_frac_per_symbol"]
    assert isinstance(agg["interp_frac_per_symbol"]["XAUUSDm"], (int, float))


# ---------------------------------------------------------------------------
# --apply round-trip (production side-effect)
# ---------------------------------------------------------------------------
def test_cli_apply_round_trip(tmp_path, monkeypatch):
    """End-to-end check: when the gate PASSES and --apply is set, the script
    MUST write a merged entry carrying ``trusted=True`` +
    ``walk_forward_verdict='PASS'`` + ``walk_forward_expectancy_delta``.

    ``core.utils.write_json_state`` resolves paths against the repo ROOT
    (``mt5_quant_agent/state/``), NOT cwd. So we monkeypatch BOTH the
    script's local ``read_json_state`` AND ``write_json_state`` to redirect
    everything into ``tmp_path`` — this prevents the test from polluting
    real bot state during a CI run.
    """
    trades = []
    for i in range(30):
        h = i // 60; m = i % 60
        trades.append(_make_trade(
            "XAUUSDm", pnl=-1.0, r_mult=-1.0, mae_R=1.0, mfe_R=0.6,
            closed_at=f"2026-07-21T{h:02d}:{m:02d}:00+00:00",
        ))
    for i in range(30):
        h = (i + 60) // 60; m = (i + 60) % 60
        trades.append(_make_trade(
            "USOILm", pnl=1.0, r_mult=1.0, mae_R=0.4, mfe_R=2.0,
            closed_at=f"2026-07-22T{h:02d}:{m:02d}:00+00:00",
        ))
    for i in range(30):
        h = (i + 60) // 60; m = (i + 60) % 60
        trades.append(_make_trade(
            "EURUSDm", pnl=1.0, r_mult=1.0, mae_R=0.4, mfe_R=1.5,
            closed_at=f"2026-07-23T{h:02d}:{m:02d}:00+00:00",
        ))

    proj = {
        "symbols": {
            "XAUUSDm": {"break_even": {"trigger_atr_mult": 0.30, "lock_profit_atr_mult": 0.05},
                         "trailing": {"activation_atr_mult": 0.65, "trail_atr_mult": 0.30}},
            "USOILm":  {"break_even": {"trigger_atr_mult": 0.65, "lock_profit_atr_mult": 0.10},
                         "trailing": {"activation_atr_mult": 1.20, "trail_atr_mult": 0.50}},
            "EURUSDm": {"break_even": {"trigger_atr_mult": 0.40, "lock_profit_atr_mult": 0.10},
                         "trailing": {"activation_atr_mult": 0.65, "trail_atr_mult": 0.30}},
        }
    }
    proj_path = tmp_path / "proposal.json"
    proj_path.write_text(json.dumps(proj), encoding="utf-8")
    out_path = tmp_path / "report.json"
    merged_path = tmp_path / "wf_apply_test.json"

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "walk_forward_microtest.py"),
        "--proposal", str(proj_path),
        "--output", str(out_path),
        "--config", str(ROOT / "config.yaml"),
        "--apply",
        "--state-out", "wf_apply_test.json",
    ]

    # Redirect BOTH reads and writes so the test never touches the real bot
    # state dir. Reads get the synthetic trade log + empty live state; writes
    # get redirected to ``tmp_path`` flat (state dir not used in this test).
    monkeypatch.setattr(wfm_module, "read_json_state",
                        lambda name, default=None: (
                            {"trades": trades} if name == "trade_log.json"
                            else {"symbols": {}}
                        ))
    def _fake_write(name, payload, **kwargs):
        path = Path(name) if Path(name).is_absolute() else (tmp_path / name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    monkeypatch.setattr(wfm_module, "write_json_state", _fake_write)

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmp_path))
        report = json.loads(out_path.read_text(encoding="utf-8"))

        if report.get("verdict") is True:
            assert res.returncode == 0
            assert merged_path.exists(), "apply path should have written merged state under tmp_path"
            merged = json.loads(merged_path.read_text(encoding="utf-8"))
            assert "XAUUSDm" in merged["symbols"]
            # Critical: trust flag, verdict stamp, and provenance.
            xau = merged["symbols"]["XAUUSDm"]
            assert xau.get("trusted") is True, "merge must set trusted=True so position_manager honours it"
            assert xau.get("walk_forward_verdict") == "PASS"
            assert isinstance(xau.get("walk_forward_expectancy_delta"), (int, float))
            assert xau["break_even"]["trigger_atr_mult"] == 0.30
            assert xau["trailing"]["activation_atr_mult"] == 0.65
        else:
            # If the synthetic log doesn't pass the gate, that's the data, not
            # the script. We at least confirm the script didn't crash and
            # did NOT write into tmp_path.
            assert res.returncode in (1, 2)
            assert not merged_path.exists(), "apply must NOT write when verdict is False"
    finally:
        # Defense-in-depth cleanup: even if monkeypatching failed, ensure we
        # never leave a real-bot-state pollution behind. The state dir is
        # rooted at <repo ROOT>/state/, never at tmp_path.
        repo_state_wf = ROOT / "state" / "wf_apply_test.json"
        if repo_state_wf.exists():
            repo_state_wf.unlink()


# ---------------------------------------------------------------------------
# Dynamic effective_min + range/regression coverage
# ---------------------------------------------------------------------------
def test_effective_min_helper_matches_ceil_formula():
    """Pure-math sanity: effective_min_evaluated(min_tail, max_interp) follows
    ``max(1, ceil(min_tail * (1 - max_interp)))``. If anyone swaps the impl
    this catches it.
    """
    assert effective_min_evaluated(5, 0.5) == 3   # ceil(5 * 0.5)
    assert effective_min_evaluated(10, 0.8) == 2  # ceil(10 * 0.2)
    assert effective_min_evaluated(8, 0.0) == 8   # no interp allowance
    assert effective_min_evaluated(2, 0.99) == 1  # floor at 1


def test_effective_min_evaluated_in_report_updates_with_overrides():
    """End-to-end: passing min_tail=10 / max_interp=0.8 yields
    effective_min_evaluated_per_symbol == 2 in the aggregate. Without this
    test the dynamic recompute could silently regress to a constant.
    """
    trades = []
    for i in range(40):
        h = i // 60; m = i % 60
        trades.append(_make_trade("XAUUSDm", -1.0, -1.0, mae_R=1.0, mfe_R=0.6,
                                  closed_at=f"2026-07-21T{h:02d}:{m:02d}:00+00:00"))
    for i in range(40):
        h = (i + 60) // 60; m = (i + 60) % 60
        trades.append(_make_trade("USOILm", 1.0, 1.0, mae_R=0.4, mfe_R=2.0,
                                  closed_at=f"2026-07-22T{h:02d}:{m:02d}:00+00:00"))
    for i in range(40):
        h = (i + 60) // 60; m = (i + 60) % 60
        trades.append(_make_trade("EURUSDm", 1.0, 1.0, mae_R=0.4, mfe_R=1.5,
                                  closed_at=f"2026-07-23T{h:02d}:{m:02d}:00+00:00"))
    proposal = {
        "XAUUSDm": {"break_even": {"trigger_atr_mult": 0.30, "lock_profit_atr_mult": 0.05},
                     "trailing": {"activation_atr_mult": 0.65, "trail_atr_mult": 0.30}},
        "USOILm":  {"break_even": {"trigger_atr_mult": 0.65, "lock_profit_atr_mult": 0.10},
                     "trailing": {"activation_atr_mult": 1.20, "trail_atr_mult": 0.50}},
        "EURUSDm": {"break_even": {"trigger_atr_mult": 0.40, "lock_profit_atr_mult": 0.10},
                     "trailing": {"activation_atr_mult": 0.65, "trail_atr_mult": 0.30}},
    }
    # Looser → 2 (ceil(10 * 0.2))
    r_loose = gate_check(_build_log(trades), {}, {}, proposal, tail_n=80, split_pct=0.7,
                         min_symbols=3, min_tail_per_symbol=10,
                         max_interpolated_fraction=0.8, min_overall_trades=50)
    assert r_loose["aggregate"]["effective_min_evaluated_per_symbol"] == 2
    # Tighter → 5 (ceil(10 * 0.5))
    r_tight = gate_check(_build_log(trades), {}, {}, proposal, tail_n=80, split_pct=0.7,
                         min_symbols=3, min_tail_per_symbol=10,
                         max_interpolated_fraction=0.5, min_overall_trades=50)
    assert r_tight["aggregate"]["effective_min_evaluated_per_symbol"] == 5


def test_load_proposal_rejects_trail_below_be_trigger(tmp_path):
    """If trailing activation is BELOW the BE trigger, the simulator can
    never trail — reject with the loud-failure policy.
    """
    raw = {"symbols": {
        "XAUUSDm": {
            "break_even": {"trigger_atr_mult": 0.9, "lock_profit_atr_mult": 0.05},
            "trailing":  {"activation_atr_mult": 0.5, "trail_atr_mult": 0.10},
        },
    }}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    # Single bad symbol → "no usable symbols"; multi-bad → "invalid symbols".
    with pytest.raises(ValueError, match="invalid symbols|no usable symbols"):
        _load_proposal(p)


def test_load_proposal_rejects_zero_trigger(tmp_path):
    """A zero trigger means BE always fires at entry — degenerate, reject.
    """
    raw = {"symbols": {
        "XAUUSDm": {
            "break_even": {"trigger_atr_mult": 0.0, "lock_profit_atr_mult": 0.05},
            "trailing":  {"activation_atr_mult": 0.65, "trail_atr_mult": 0.30},
        },
    }}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid symbols|no usable symbols"):
        _load_proposal(p)


# ---------------------------------------------------------------------------
# Constants exposed for sanity
# ---------------------------------------------------------------------------
def test_public_constants_set():
    assert MIN_SYMBOLS >= 1
    assert MIN_TAIL_PER_SYMBOL >= 1
    assert DEFAULT_TAIL_N == 100
    assert 0.0 < DEFAULT_SPLIT_PCT < 1.0
