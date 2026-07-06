"""SQLite schema for Phase 2 state store."""

from __future__ import annotations

SCHEMA_VERSION = 3

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    setup_type TEXT,
    confidence REAL,
    entry REAL,
    sl REAL,
    tp1 REAL,
    tp2 REAL,
    entry_quality REAL,
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);

CREATE TABLE IF NOT EXISTS approved_signals (
    signal_id TEXT PRIMARY KEY,
    approved_at TEXT NOT NULL,
    verifier_result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejected_signals (
    signal_id TEXT PRIMARY KEY,
    rejected_at TEXT NOT NULL,
    reason TEXT,
    verifier_result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    signal_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT,
    entry REAL,
    sl REAL,
    tp REAL,
    status TEXT,
    mt5_ticket TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_signal ON orders(signal_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    size REAL,
    entry REAL,
    sl REAL,
    tp REAL,
    mt5_ticket TEXT,
    status TEXT,
    opened_at TEXT,
    updated_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry REAL,
    exit REAL,
    pnl REAL,
    setup_type TEXT,
    opened_at TEXT,
    closed_at TEXT,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);

CREATE TABLE IF NOT EXISTS risk_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    symbol TEXT,
    severity TEXT,
    message TEXT,
    created_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    symbol TEXT,
    details_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluated_signals (
    signal_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    action TEXT,
    policy_score REAL,
    created_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evaluated_action ON evaluated_signals(action);

CREATE TABLE IF NOT EXISTS policy_scores (
    policy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    setup_type TEXT,
    session TEXT,
    entry_type TEXT,
    limit_offset_atr REAL,
    sl_atr_mult REAL,
    tp1_r REAL,
    be_trigger_r REAL,
    trail_start_r REAL,
    score REAL,
    sample_n INTEGER,
    created_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (policy_id, symbol, setup_type, session)
);

CREATE INDEX IF NOT EXISTS idx_policy_scores_symbol ON policy_scores(symbol);
CREATE INDEX IF NOT EXISTS idx_policy_scores_score ON policy_scores(score);
"""