# Phase 2.3 - Fast Scalper Layer (two-speed: slow brain -> fast hands)

Back to [README](../README.md) - [Phase 2](PHASE2_UPDATE.md) - [Phase 1](PHASE1_UPDATE.md) - [Structure](STRUCTURE.md)

Phase 2.3 adds a tick-reactive scalping layer on top of the existing evaluation/verifier pipeline. The "slow brain" (evaluation_loop + verifier) still decides *what* to trade; the "fast hands" react to live ticks to *enter, manage and exit* within the window the slow brain approved. Fast mode is **off by default** (`config.yaml` -> `fast_mode.enabled: false`) and **observe-only** until a runtime preset flips `live_enabled`.

## Components

| Module | Role |
|--------|------|
| `core/fast_mode.py` | Config helpers - `fast_mode_enabled`, `fast_mode_live`, `fast_mode_symbols`, `tick_interval_seconds` |
| `core/fast_mode_runtime.py` | Dashboard/TUI presets (`off` / `observe` / `safe` / `aggressive` / `sprint`) persisted to `state/fast_mode_runtime.json`, merged over profile YAML |
| `core/fast_signal_cache.py` | Slow brain writes the execution cache (`fast_signal_cache.json`); fast hands read it. Tracks 1h + 10m trade buckets, consecutive losses, loss cooldown |
| `core/microstructure.py` | Tick-level checks - spread gate, anchor distance (ATR), entry zone, lightweight momentum score |
| `core/fast_entry_executor.py` | Per-tick `would_enter_*` / `enter_*` decision with all safety gates |
| `core/fast_live_executor.py` | Places the MT5 order when a live `enter_*` decision fires (verifier-gated, kill-switch & blue-guardian aware) |
| `loops/fast_tick_loop.py` | Supervisor service - reads ticks, evaluates entries, delegates to live executor |
| `loops/fast_position_guard.py` | Supervisor service - fast break-even / trail / emergency-exit observe actions (live MT5 path delegates SL mods to `position_manager`) |

## Safety gates (fast_entry_executor.evaluate_entry)

Checked in order; the first failure sets `action = blocked` (or `wait`/`cancel`):

1. `kill_switch.json` kill flag
2. `health.json` degraded
3. `max_open_positions` per symbol
4. Rate limits - `max_trades_per_symbol_per_hour`, `max_trades_per_10min` (sprint cap), `max_consecutive_losses_per_symbol`, `cooldown_after_loss_seconds`
5. Spread gate - `max_spread_mult` vs feature baseline spread
6. Momentum - `min_tick_momentum_score`
7. Anchor drift - `max_anchor_distance_atr` (too far -> `cancel`)
8. Entry zone / market snap - `trigger_zone_atr`, `allow_limit_entries`, `allow_market_entries`, `market_if_distance_atr_below`

The 10-minute cap (`max_trades_per_10min`) is honoured by the `sprint` preset (default `2`) and any runtime override that sets it; `record_trade_entry` maintains a rolling 10-minute window in `state/fast_mode_state.json`.

## Runtime presets

Switch mode without editing YAML from the dashboard (`/api/fast-mode`), the terminal web view buttons, or by writing `state/fast_mode_runtime.json`:

| Preset | live | tick | symbols | market | notes |
|--------|------|------|---------|--------|-------|
| off | no | - | - | no | fast layer disabled |
| observe | no | 1s | XAU/USOIL/UK100 | no | log `would_*` decisions only |
| safe | yes | 1s | XAU/USOIL/UK100 | no | verifier-gated limit entries |
| aggressive | yes | 500ms | XAU/USOIL/UK100 | yes | market snap when spread/zone OK |
| sprint | yes | 250ms | XAU only | yes | 1 position, 2 trades / 10 min, emergency exit on |

Tick-interval changes need a bot restart (supervisor interval is fixed at registration); live/symbol changes apply on the next fast tick.

## MT5 pending-order support

`core/mt5_broker.py` now records pending (limit/stop) orders with `status = "pending"`, `fill_price = None` and accepts `TRADE_RETCODE_PLACED` alongside `TRADE_RETCODE_DONE`. Pending orders count as "submitted" for de-dup so the fast layer does not re-fire the same `signal_id` while the limit rests. `strategy_entry.resolve_entry_mode` honours `trading.strategy_entries.use_limit_orders: false` to force market entries.

## Observability

- Dashboard: **Fast Scalper** card (cache rows, latest tick decisions, position-guard actions) + **Fast Scalper Presets** card in settings.
- Terminal TUI: `fast scalper` section + web preset buttons on `:8083`.
- `scripts/preflight.py` prints fast-mode mode/symbols and warns when `live_enabled`.
- SQLite: `fast_mode_decisions` table (`state_schema` v4) via `StateStore.append_fast_decision`.

## Known limitations / future work

- `emergency_exit.spread_spike_mult` is a config knob for the sprint preset but is **not yet wired** - the guard has no live spread source in the observe path and the adverse-move half currently only emits observe actions (no real close in either path). Spread-spike emergency exit is tracked as follow-up work.
- `fast_position_guard` live MT5 path delegates BE/trail SL modifications to `position_manager`; a true emergency-close path is not implemented yet.
- Fast mode requires MT5 execution mode for live orders; observe mode runs on the `features.json` snapshot.

## Tests

`tests/test_fast_mode.py` covers the cache build, entry gates (kill-switch, momentum, zone, limit-force, 10-min cap), runtime preset overrides, verifier-gated live blocking, and the pending-order record shape.
