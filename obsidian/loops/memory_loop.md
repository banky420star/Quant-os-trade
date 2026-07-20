# memory_loop

**File:** `loops/memory_loop.py`

## Inputs

- `paper_trades.json` (new closes)
- `approved_signals.json` — enrichment via `core/trade_enrichment.py`

## Outputs

- `memory.json`
- `edge_scores.json`
- Edge DB ingest
- Arena: `record_outcomes` → `strategy_arena.json` + `arena_insights.json`

## Related

- [[Strategy Arena]]
- [[memory_loop]] → [[adaptation_loop]] (new close count)