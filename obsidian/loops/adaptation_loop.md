# adaptation_loop

**File:** `loops/adaptation_loop.py` · **Log:** `logs/adaptation_loop.log`

## Replaces

Legacy `forward_test_loop` as a top-level pipeline step. Culturing still runs inside via `forward_test_loop.run()`.

## Every cycle

- Refresh `forward_test_ledger.json`
- Update `symbol_policy_live.json` vetoes

## On new closes

- Trade log rebuild
- BE/trail calibration
- `adaptation_log.json` diff report

## Related

- [[Adaptation]]
- [[verifier_loop]] reads vetoes next cycle