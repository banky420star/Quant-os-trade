# signal_loop

**File:** `loops/signal_loop.py` · Decision / evidence engine

## Inputs

- `features.json`
- `market_context.json`
- `edge_scores.json`

## Outputs

- `candidate_signals.json`
- `strategy_rankings.json`
- Arena: `record_triggers` → `strategy_arena.json`
- `write_setup_catalog` → `setup_catalog.json`

## Related

- [[Strategy Arena]]
- `core/decision_engine.py`
- `core/setup_classifier.py`