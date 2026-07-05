# State Files

All under `mt5_quant_agent/state/`. Written atomically via `core/utils.write_json_state`.

## Orchestration

| File | Writer | Reader |
|------|--------|--------|
| `supervisor.json` | Supervisor heartbeat | Dashboard, TUI, coordination_check |
| `runtime_mode.json` | `start.py` | All loops via `load_config` |
| `health.json` | [[health_loop]] | Dashboard, coordination_check |
| `coordination.json` | `coordination_check.py --write-state` | Ops |

## Market data

| File | Loop |
|------|------|
| `latest_candles.json` | [[data_loop]] |
| `broker_symbols.json` | [[data_loop]] |
| `features.json` | [[feature_loop]] |
| `market_context.json` | [[market_context_loop]] |

## Trading

| File | Loop |
|------|------|
| `risk_state.json` | [[risk_loop]] |
| `kill_switch.json` | [[risk_loop]] |
| `daily_growth.json` | [[risk_loop]] |
| `candidate_signals.json` | [[signal_loop]] |
| `approved_signals.json` | [[verifier_loop]] |
| `rejected_signals.json` | [[verifier_loop]] |
| `paper_orders.json` | [[execution_loop]] |
| `paper_positions.json` | [[execution_loop]] |
| `paper_trades.json` | closes from execution / position manager |
| `position_management.json` | [[position_manager_loop]] |

## Intelligence

| File | Loop |
|------|------|
| `strategy_arena.json` | [[signal_loop]], [[memory_loop]] |
| `setup_catalog.json` | [[signal_loop]] |
| `arena_insights.json` | arena analytics |
| `edge_scores.json` | [[memory_loop]] |
| `memory.json` | [[memory_loop]] |
| `forward_test_ledger.json` | [[adaptation_loop]] |
| `trade_log.json` | [[trade_log_loop]] |

## Campaign

| File | Purpose |
|------|---------|
| `growth_campaign.json` | Active growth campaign |
| `strategy_arena.json` | Arena campaign_id + leaderboard |