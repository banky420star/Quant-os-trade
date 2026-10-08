# JEV active in Quant OS — paper-only integration

## What is wired

The normal `loops/evaluation_loop.py` now calls
`core.jev_shadow_runtime.evaluate_candidates()` **after** the existing
Quant evaluation policy and **before** the existing verifier.

- `jev.enabled: false`: original behavior, no JEV calls.
- `jev.mode: shadow`: ask JEV and archive its verdict; pass every original
  evaluated signal onward, unchanged.
- `jev.mode: paper_gate`: if and only if
  `execution.mode: paper` **and** `live_trading_enabled: false`,
  require a genuine OpenRouter JEV decision agreeing with the candidate
  side. Otherwise veto the paper candidate.
- Live MT5 mode is always observational; JEV never changes MT5 order inputs.
- Hard risk, entry confirmation, exposure and kill switch remain downstream.
- Heuristic fallback, provider errors or inference-budget exhaustion are
  never treated as a real JEV trade approval in paper_gate mode.
- Cap on new JEV calls per cycle avoids unbounded API spending/latency.

## Local Mac test profile

The isolated clone on the AI drive has an **ignored** `config.local.yaml`
that explicitly sets:

```yaml
execution:
  mode: paper
  live_trading_enabled: false
  mt5_trading_enabled: false
  allow_live_account: false
state_store:
  enabled: false
jev:
  enabled: true
  mode: paper_gate
  provider: openrouter
  model: typesafe/jev-1.13
  timeout_ms: 4000
  max_candidates_per_cycle: 3
  cache_seconds: 15
  allow_heuristic_fallback: false
```

`jev.env_file` points to an already configured local credential file. No
credentials are in this repository.

**Important:** the upstream `config.yaml` defaults to MT5 execution. Do not
launch `start.py` with that base configuration when you intend paper only.
Verify the effective loaded configuration first:

```bash
python3 -c "from core.utils import load_config; c=load_config(); print(c['execution']['mode'],c['execution']['live_trading_enabled'],c.get('jev'))"
```

## Commands from repository root

Run the code tests:

```bash
python3 -m pytest -q tests/test_jev_shadow.py tests/test_jev_paper_gate.py tests/test_jev_evaluation_wiring.py
```

Run a **synthetic snapshot** through genuine JEV + the existing Quant
verifier and paper broker, without persisting fake outcomes:

```bash
python3 scripts/jev_paper_demo.py
```

Run one shadow pass on **real existing state files**:

```bash
python3 scripts/jev_shadow_once.py
```

For the normal Quant OS evaluation cycle, the new JEV gate is called from
`loops.evaluation_loop.run` when `jev.enabled` is true. The rest of the
pipeline (verifier, execution) is unchanged.

When baseline trades and archived **shadow-mode** decisions with matching
signal IDs are available, inspect the comparison:

```bash
python3 scripts/jev_ab_report.py
```

## Audit files

- `state/jev_decisions.json`: latest state snapshots and JEV verdicts.
- `state/jev_decision_archive.jsonl`: historical deduplicated observations.
- `state/jev_ab_report.json`: A/B comparison, or explicit insufficient-data
  status when not supportable.

The A/B report excludes paper-gate decisions: vetoed orders have no realized
fill and so cannot be assigned a made-up P&L. A proper parallel paper baseline
or a sufficiently long shadow period is required to estimate performance.

## Safety and data limitations

Quant OS relies on MT5 candle/spread feeds. The Mac test clone has no genuine
MT5 history or live Quant state; a synthetic simulation is a **plumbing test**
only, not a profitable backtest. The hard verifier can legitimately reject all
signals because of exposure, spread, confirmation, or regime even when JEV
agrees with them.

Do not interpret raw JEV action scores as calibrated win probabilities until
validated against held-out closed outcomes. Never automatically promote this
paper gate into a live account on the basis of smoke tests.
