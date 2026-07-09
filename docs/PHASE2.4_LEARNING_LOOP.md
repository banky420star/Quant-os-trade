# Phase 2.4 - Normalized Learning Loop

Back to [README](../README.md) - [Phase 2.3 (Fast Scalper)](PHASE2.3_FAST_MODE.md) - [Structure](STRUCTURE.md)

A review-and-proposal loop that turns messy trade logs into structured data an
LLM/ML model can learn from, scores every closed trade, diagnoses *why* it
won/lost, and proposes **bounded** config changes. Live self-modification is
**blocked by default**.

## The loop

```
record -> normalize -> score -> diagnose -> propose -> test/shadow -> bounded patch -> monitor -> rollback if worse
```

The bot does NOT directly rewrite its own live config. Default mode is
`observe_only`; nothing is applied until you explicitly move up the mode ladder.

## Modes (`learning.mode` in config.yaml)

| Mode | What happens |
|------|--------------|
| `observe_only` (default) | Log every decision; no review, no proposal, no apply |
| `review_only` | Score closed trades, update `learning_state.json` |
| `propose_only` | Also write bounded config proposals to `logs/config_proposals.jsonl` |
| `shadow_apply` | Simulate proposals against a config copy (no live change) |
| `live_apply_limited` | Apply tiny, low-risk, auto-allowed patches to a runtime override file, with rollback if performance gets worse |

## 1. Normalized event logging (`core/learning_logger.py`)

Every bot action is appended as one-line JSON to `logs/*.jsonl`:

- `decisions.jsonl`, `orders.jsonl`, `position_management.jsonl`, `outcomes.jsonl`,
  `errors.jsonl`, `reviews.jsonl`, `config_changes.jsonl`, `config_proposals.jsonl`

Files are thread-safe and rolling-capped (8000 lines). Decisions include skips,
not only trades, so the reviewer can learn from trades NOT taken.

A decision record: `timestamp, symbol, timeframe, mode, price, spread_points,
atr, side, confidence, decision(market_order|limit_order|skip), guards{...},
reason, config_hash, profile`.

## 2. Schemas & categories (`core/learning_schema.py`)

Categories: `market_context, signal_context, order_decision, execution_quality,
position_management, outcome, mistake_type, learning_rating, config_proposal,
config_patch_result`. `config_snapshot_hash()` stamps each event with the config
slice that produced it.

## 3. Trade reviewer (`core/trade_reviewer.py`)

Scores each closed trade (entry_timing, exit_quality, risk, trend_alignment,
execution -> `rating_total` 0-100) and emits mistake categories:

`tp_too_early, tp_too_far, sl_too_tight, entry_late, wrong_timeframe_alignment,
spread_spike_entry, spread_spike_exit, overtrading, chop_zone_entry,
poor_limit_distance, bad_session, missed_trade_after_skip`.

- **tp_too_early**: detected from post-exit price movement (price kept going the
  trade way by >=0.3 ATR after exit) OR from MFE-vs-realized R (left >=0.5R on
  the table).
- **sl_too_tight**: stopped on a small adverse wiggle (MAE > -0.2R) then price
  reversed into the trade direction.
- **wrong_timeframe_alignment**: M5 and M15 trends disagreed at entry.

All detectors degrade gracefully on missing data (sparse historical trades).

## 4. Config proposals (`core/config_proposal.py`)

Aggregates reviews into bounded proposals (one per symbol+pattern). Deltas are
tiny and clamped, e.g. `tp1_rr +0.05..+0.15`, `sl_atr_mult +0.1`,
`min_confidence +5`, `spread_mult -0.2`, `max_trades_per_hour -1`.

### Safety policy (always enforced)
The engine may **never** auto-change: lot size, `live_trading_enabled`,
`account_mode`, credentials, `magic_number`, `starting_cash`, `max_drawdown_pct`,
`kill_switch`. It may **never disable** the emergency-exit, break-even, or
news guard. Spread-guard loosening and trade-cap increases are rejected (cap
relax needs >=30 reviewed trades proving improvement). Dangerous proposals are
marked `rejected` and never applied.

## 5. Learning state (`state/learning_state.json`)

Tracks rolling win rate, rolling expectancy (R), rolling drawdown, avg rating by
symbol/setup, mistake counts, recent ratings, and active/rejected/applied
proposals + rollback triggers.

## 6. Loop (`loops/learning_review_loop.py`)

Registered as a supervisor service in `start.py` (interval
`learning.loop_interval_seconds`, default 120s). It reviews only NEW closes
since the last run (tracked by `last_reviewed_trade_id`), so it is cheap to run
continuously. It **never places trades** and contains no broker/execution code.

`live_apply_limited` writes to `state/learning_config_overrides.json` (a runtime
override file), **never to config.yaml**, and only for low-risk auto-allowed
proposals. Rollback is triggered when rolling performance degrades after an
apply (the loop records a rollback marker and drops the patch).

## 7. Dashboard

A **Learning Loop** card shows: current mode + auto-apply disabled/enabled,
reviewed count, rolling win%/expectancy, last 10 decisions, last 10 trade
reviews (with rating + mistakes), mistake counts, and active vs rejected
proposals.

## Running / inspecting

- Default (safe): `learning.mode: observe_only` - logs only.
- Review: set `learning.mode: review_only` and restart the bot.
- Inspect proposals: `logs/config_proposals.jsonl` and `state/learning_state.json`.
- Apply nothing risky: keep `live_apply_limited` off until you trust the proposals.

## Why live self-modification is blocked by default

A self-mutating trading bot with admin rights is dangerous. The design is
intentionally `observe -> review -> propose -> shadow -> bounded apply ->
rollback`. The bot behaves like: *"I observed 47 trades, found 13 early exits,
propose raising TP ATR 1.20 -> 1.30, shadow-tested +8%, apply? yes/no."* You
flip the mode; the engine does the rest safely.

## Tests

`tests/test_learning_logger.py`, `tests/test_trade_reviewer.py`,
`tests/test_config_proposal.py`, `tests/test_learning_loop.py` - prove
normalization, scoring, tp_too_early/sl_too_tight/wrong-tf/spread/overtrading
detection, bounded proposals, rejection of dangerous proposals, that the loop
never places trades, and that observe_only cannot modify config.
