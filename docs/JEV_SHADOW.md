# JEV Shadow Integration

This branch adds a **read-only decision layer** for evaluating TypeSafe JEV 1.13
against Quant OS signals. It does not alter the normal pipeline and does not place,
close, resize, or modify trades.

## Files

- `core/jev_decision_bus.py` — normalized BUY/SELL/WAIT JEV adapter.
- `core/jev_ab_report.py` — conservative historical A/B scorer.
- `scripts/jev_shadow_once.py` — evaluates the current candidate/evaluated signals.
- `scripts/jev_ab_report.py` — matches stored shadow decisions to closed trades.
- `tests/test_jev_shadow.py` — decision and counterfactual-safety tests.

## Provider

Set a working OpenRouter Decisions API key:

```bash
export OPENROUTER_API_KEY="..."
```

Optional `config.local.yaml`:

```yaml
jev:
  enabled: true
  mode: shadow
  provider: auto
  model: typesafe/jev-1.13
  timeout_ms: 4000
  allow_heuristic_fallback: true
```

If no provider key is available, the runner uses a deterministic heuristic fallback
so plumbing and reports can still be tested. The output clearly records the provider.

## Run

Generate current shadow decisions:

```bash
python scripts/jev_shadow_once.py
```

Output:

`state/jev_decisions.json`

After the corresponding trades have closed and `state/trade_log.json` exists:

```bash
python scripts/jev_ab_report.py
```

Output:

`state/jev_ab_report.json`

Run tests:

```bash
python -m pytest -q tests/test_jev_shadow.py
```

## A/B methodology

The comparison is deliberately conservative.

- Baseline = real historical Quant OS trades.
- JEV shadow = the subset where JEV agreed with the original BUY/SELL direction.
- JEV WAIT or opposite-side opinion = **veto**.
- Opposite-side hypothetical trades are never invented because there is no real fill,
  spread, slippage, management path, or realized P&L for that counterfactual.

The report includes:

- matched trades
- baseline vs JEV-veto trade count
- win rate
- total/average P&L
- profit factor
- max drawdown
- average R when available
- losses avoided
- winners skipped
- deltas

## Promotion rule

Do not wire JEV into execution based on a single session. A reasonable promotion
review should require enough matched closed trades across multiple symbols/regimes,
stable provider latency, and an improvement in expectancy/drawdown that survives
out-of-sample comparison.

This branch intentionally contains no execution hook.
