# Quant OS Cross-Repo Harvest Integration

Branch: `agent/quant-harvest-integration`

## Purpose

Quant OS remains the only production/runtime spine. This branch selectively
reimplements high-value research and model-governance ideas found in
`super-lamp` and `supreme-chainsaw` without importing their legacy execution,
operator-control, runtime-state, or auto-live-promotion behavior.

## Non-negotiable boundary

All harvested intelligence is **shadow-only** until it passes Quant OS's own
future validation and promotion lifecycle.

A research candidate may:

- carry immutable dataset / feature / code provenance;
- be evaluated against deterministic promotion gates;
- be copied into an immutable content-addressed artifact store;
- be registered as a challenger;
- be staged or rolled back as a shadow canary;
- receive the same immutable market snapshots as the research champion;
- produce telemetry and comparison evidence;
- become eligible for a human research-review proposal after sufficient paired evidence.

A research candidate may **not**:

- open the global MT5 execution gate;
- call `order_send`;
- enqueue executable intents;
- change position sizing used by live/demo execution;
- change kill-switch state;
- invoke dashboard resume/unblock controls;
- promote itself to real-money execution;
- modify live profile authority flags.

## Harvest map

### Quant OS remains authoritative for

- process-global MT5 ownership;
- execution serialization;
- Phase 0 authority gates;
- reset safety;
- dashboard mutation authorization;
- M1 structure lifecycle and stale-data fail-closed behavior;
- runtime supervision and CI.

### Reimplemented from super-lamp concepts

- feature-set and dataset provenance;
- matrix fingerprints for ablation verification;
- purged walk-forward evidence contracts;
- point-in-time and leakage checks;
- after-cost and doubled-cost stress evidence;
- regime breakdowns and baseline comparisons;
- deterministic promotion gates;
- uncertainty-aware challenger evidence review.

### Reimplemented from supreme-chainsaw concepts

- challenger / canary model lifecycle;
- immutable content-addressed model objects;
- artifact integrity hashes;
- model/version provenance;
- shadow rollback and lifecycle audit history;
- same-snapshot champion/challenger comparison.

No files are copied wholesale from either repository. The integration uses
Quant OS naming, safety semantics, tests, and state ownership.

## Current implementation

### `core/model_governance.py`

Provides:

- `ProvenanceManifest`
- `ValidationArtifact`
- `PromotionThresholds`
- `PromotionDecision`
- `PromotionPolicy`
- `ShadowModelRegistry`
- feature/dataset/artifact SHA-256 helpers

A passing `PromotionPolicy` result means only:

> eligible for shadow canary evaluation

Every `PromotionDecision` contains:

```text
shadow_only = true
execution_authority_granted = false
```

The registry rewrites those safety fields on persisted state so a caller cannot
smuggle execution authority through a crafted state payload.

### `core/model_artifacts.py`

Provides:

- immutable content-addressed artifact objects;
- hashes binding model files, provenance and validation evidence;
- SHA-256 verification before publication;
- re-verification before shadow stage/rollback;
- re-verification immediately before a shadow evaluator loads a file;
- path-traversal and candidate-identity checks;
- append-only shadow lifecycle audit records;
- Windows-safe durability flushing for copied model payloads.

A modified or missing artifact fails closed and cannot be staged or loaded.

### `research/validation/feature_ablation.py`

Provides named, shape-preserving feature ablations with deterministic DataFrame
fingerprints. A declared ablation raises immediately when it produces the same
matrix as the control, catching broken column maps and no-op masks before an
expensive training run is trusted.

The same evaluator receives a deep copy of the control and each variant. The
report records control/variant fingerprints, metrics, numeric deltas and one
content-addressed evidence hash.

### `research/validation/quant_harness.py`

Builds the canonical `ValidationArtifact` from explicit research evidence:

- net and gross validation returns;
- observed after-cost return, profit factor, sample Sharpe and drawdown;
- doubled-cost stress using the observed gross-to-net cost difference;
- point-in-time feature audit;
- future-target leakage scan;
- walk-forward windows passed;
- regime sample coverage;
- random-policy and previous-champion baselines;
- test, account-telemetry and real-money-lock attestations.

Missing gross returns, features, targets, baselines, regimes, walk-forward
windows, tests or telemetry do not receive optimistic defaults. They produce a
failing artifact. The harness never assigns subjective confidence values.

A validation bundle is written atomically and always persists:

```text
shadow_only = true
execution_authority_granted = false
```

### `core/shadow_challenger.py`

Runs the research champion and challenger against independent copies of one
content-addressed immutable market snapshot.

The service:

- verifies both model artifacts before either evaluator runs;
- issues one shared trace ID and snapshot ID;
- measures per-model evaluation latency;
- normalizes decisions to `BUY`, `SELL`, or `WAIT`;
- rejects invalid actions;
- detects evaluator mutation of its input snapshot;
- converts evaluator exceptions and invalid inputs to `WAIT`;
- classifies agreement, conflict and unavailable decisions;
- appends comparison evidence without storing raw market payloads.

The comparison object has no executable intent and always forces:

```text
shadow_only = true
execution_authority_granted = false
```

### `core/post_canary_policy.py`

Evaluates paired, realized shadow outcomes after a challenger has accumulated
forward evidence. It derives minimum sample requirements from the strategy's
expected observation frequency instead of using one universal trade count.

The policy requires:

- sufficient paired observations and active market days;
- independently represented regimes;
- high outcome coverage;
- low challenger error rate;
- positive challenger total reward;
- a positive lower confidence bound from a deterministic paired bootstrap;
- acceptable drawdown degradation;
- acceptable evaluation latency;
- unique trace IDs and valid shadow-only observations.

A pass produces only:

```text
eligible_for_operator_review = true
operator_review_required = true
live_promotion_eligible = false
shadow_only = true
execution_authority_granted = false
```

The review proposal is content-addressed and written atomically. No method in
the module can stage a live model or change runtime authority.

## Validation principles

Promotion fails closed when evidence is missing or weak. Current checks cover:

- MT5 source requirement;
- spread data availability;
- leakage detection;
- feature-audit result;
- after-cost return;
- profit factor;
- Sharpe;
- drawdown;
- trade sample size;
- single-trade concentration;
- walk-forward windows;
- regime breakdown;
- doubled-cost stress;
- random-policy baseline;
- previous-champion baseline;
- test status;
- account telemetry validity;
- real-money lock;
- immutable artifact integrity;
- post-canary active days and paired sample coverage;
- paired bootstrap uncertainty;
- challenger error, latency and drawdown guards.

The numerical defaults are entry-to-canary or operator-review gates, not claims
that a candidate is safe or statistically proven for live capital.

## Tests and CI

The harvest test group covers:

- deterministic provenance and content fingerprints;
- fail-closed promotion gates;
- shadow-only registry state;
- immutable artifact tamper detection;
- re-verification before stage/load;
- shadow rollback audit history;
- missing-evidence failure behavior;
- feature-ablation no-op detection;
- evaluator isolation;
- cost-stress and regime failures;
- baseline and real-money-lock enforcement;
- identical independent snapshots for champion and challenger;
- evaluator exception, mutation and invalid-action fallback to `WAIT`;
- artifact-integrity blocking before model evaluation;
- append-only comparison evidence with no order/intent fields;
- frequency-aware post-canary sample requirements;
- active-day, regime, coverage, error, latency and drawdown gates;
- deterministic paired-bootstrap evidence;
- operator-review proposals that can never grant execution authority.

`.github/workflows/phase0-ci.yml` compiles the harvested modules and runs their
broker-agnostic tests in a dedicated Linux job while the existing Windows
Phase 0, M1/feed and broader regression jobs remain unchanged. Pull requests
into `agent/p0-safety-closure` are included explicitly in the CI trigger.

## Next integration slices

1. Research orchestration
   - generate experiment proposals and validation jobs;
   - never mutate production configuration directly;
   - never auto-promote beyond shadow roles.

2. Dashboard research observability
   - show challenger artifact, data provenance and validation hash;
   - show same-snapshot decision agreement/conflict rates;
   - show operator-review evidence without providing an approval or execution control;
   - remain read-only.

3. Deterministic replay bridge
   - bind market snapshots, decisions, validation evidence and artifact hashes;
   - reproduce one shadow comparison from its trace ID;
   - remain disconnected from execution.

## Release rule

This harvest branch must not be merged into the Phase 0 release candidate until:

- its tests pass in GitHub CI;
- existing Phase 0 safety/M1/data-feed tests remain green;
- no harvested module imports broker execution code;
- no new route can change execution authority;
- Windows validation still proves zero unintended orders.
