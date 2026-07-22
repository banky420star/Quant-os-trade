# Adaptation

Loop: [[adaptation_loop]] · Replaces standalone `forward_test_loop` in the pipeline.

## Every cycle

1. Runs `loops/forward_test_loop.py` internally → `forward_test_ledger.json`
2. Updates culturing vetoes → `symbol_policy_live.json`
3. Verifier reads vetoes on next [[signal_loop]] cycle

## On new closes only

1. Rebuild trade log
2. Calibrate BE/trail → `symbol_be_trail_live.json`
3. Log changes → `adaptation_log.json`

## State

| File | Role |
|------|------|
| `forward_test_ledger.json` | Per-cell win rate / expectancy |
| `symbol_policy_live.json` | Active vetoes |
| `symbol_be_trail_live.json` | Calibrated exit params |
| `adaptation_state.json` | Memory count baseline |
| `adaptation_log.json` | Change history |

## Dashboard / TUI

Culturing panel reads ledger cells. Min sample + veto thresholds in `config.yaml` → `culturing`.