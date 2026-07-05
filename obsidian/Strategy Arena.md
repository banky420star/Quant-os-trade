# Strategy Arena

All 8 setups compete per symbol. Winners earn leaderboard points.

## Setups

1. trend_continuation
2. pullback
3. breakout
4. compression_breakout
5. range_fade
6. liquidity_sweep
7. mean_reversion
8. false_breakout

Catalog: `state/setup_catalog.json` · Triggers: `core/setup_triggers.py`

## Wiring

| Event | Loop | Function |
|-------|------|----------|
| Signal fired | [[signal_loop]] | `record_triggers` |
| Trade closed | [[memory_loop]] | `record_outcomes` |
| Analytics | `core/arena_analytics.py` | `persist_insights` → `arena_insights.json` |

## Config (`strategy_arena`)

- `emit_all_setups: true` — every classifier hit becomes a candidate
- `max_open_per_symbol: 5`
- `max_open_per_setup: 2`
- Points: win +10, loss −4, +5 per R

## Insights dimensions

- By setup × session
- By symbol × setup
- By symbol × session
- Condition cells (regime + volatility + session)

Log: `logs/strategy_arena.log` · State: `state/strategy_arena.json`