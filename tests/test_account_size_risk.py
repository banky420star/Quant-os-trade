"""Account-size-aware per-trade risk cap.

The per-trade dollar risk must scale with the live account equity, not sit at a
fixed dollar figure that means 33% on a $30 account and 0.3% on a $3,000 one.
"""

from __future__ import annotations

from core.risk_cap import (
    effective_risk_cap,
    has_per_trade_cap,
    risk_per_trade_cap,
    risk_per_trade_pct,
)


def test_percent_cap_scales_with_equity():
    cfg = {"risk": {"max_risk_per_trade_pct": 1.0}}
    assert risk_per_trade_cap(cfg, equity=30) == 0.30
    assert risk_per_trade_cap(cfg, equity=300) == 3.0
    assert risk_per_trade_cap(cfg, equity=3000) == 30.0


def test_percent_and_static_take_the_tighter():
    cfg = {"risk": {"max_risk_per_trade_pct": 2.0, "max_loss_per_trade_usd": 10}}
    # 2% of $300 = $6 < $10 static -> percent binds
    assert risk_per_trade_cap(cfg, equity=300) == 6.0
    # 2% of $1000 = $20 > $10 static -> static ceiling binds
    assert risk_per_trade_cap(cfg, equity=1000) == 10.0


def test_explicit_per_symbol_cap_is_authoritative():
    # An explicit per-symbol dollar cap is the operator's deliberate choice and
    # is NOT tightened by the global percentage.
    cfg = {
        "risk": {"max_risk_per_trade_pct": 1.0},
        "signals": {"symbol_rules": {"XAUUSDm": {"max_loss_per_trade_usd": 21}}},
    }
    assert risk_per_trade_cap(cfg, symbol="XAUUSDm", equity=37) == 21.0
    # A symbol without an explicit rule still gets the account-aware percent.
    assert risk_per_trade_cap(cfg, symbol="EURUSDm", equity=37) == 0.37


def test_no_equity_falls_back_to_static_only():
    # Backward compatible: callers that don't pass equity get only static caps.
    cfg = {"risk": {"max_risk_per_trade_pct": 1.0, "max_loss_per_trade_usd": 8}}
    assert risk_per_trade_cap(cfg) == 8.0
    cfg_pct_only = {"risk": {"max_risk_per_trade_pct": 1.0}}
    assert risk_per_trade_cap(cfg_pct_only) is None  # nothing priceable without equity


def test_effective_risk_cap_prices_percent_against_balance():
    cfg = {"risk": {"max_risk_per_trade_pct": 1.0}}
    # effective_risk_cap passes balance through as the equity to price the %.
    assert effective_risk_cap(cfg, 500.0) == 5.0
    assert effective_risk_cap(cfg, 5000.0) == 50.0


def test_has_per_trade_cap_detects_percent():
    assert has_per_trade_cap({"risk": {"max_risk_per_trade_pct": 1.0}}) is True
    assert has_per_trade_cap({"trading": {"max_loss_per_trade_usd": 5}}) is True
    assert has_per_trade_cap({}) is False


def test_risk_per_trade_pct_precedence():
    # per-symbol percent overrides the global percent
    cfg = {
        "risk": {"max_risk_per_trade_pct": 1.0},
        "signals": {"symbol_rules": {"XAUUSDm": {"max_risk_per_trade_pct": 0.5}}},
    }
    assert risk_per_trade_pct(cfg, "XAUUSDm") == 0.5
    assert risk_per_trade_pct(cfg, "EURUSDm") == 1.0
