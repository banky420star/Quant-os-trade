# Quant OS Cross-Repo Harvest Integration

Branch: `agent/quant-harvest-integration`

## Purpose

Quant OS remains the only production/runtime spine.  This branch selectively
reimplements high-value research and model-governance ideas found in
`super-lamp` and `supreme-chainsaw` without importing their legacy execution,
operator-control, runtime-state, or auto-live-promotion behavior.

## Non-negotiable boundary

All harvested intelligence is **shadow-only** until it passes Quant OS's own
future validation and promotion lifecycle.

A research candidate may:

- carry immutable dataset / feature / code provenance;
- be evaluated against deterministic promotion gates;
- be registered as a challenger;
- be staged as a shadow canary;
- produce telemetry and comparison evidence.

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
- standardized validation evidence;
- deterministic promotion gates;
- future walk-forward / regime / stress evidence contracts.

### Reimplemented from supreme-chainsaw concepts

- challenger / canary model lifecycle;
- artifact integrity hashes;
- model/version provenance;
- rollback/history concepts.

No files are copied wholesale from either repository.  The integration uses
Quant OS naming, safety semantics, tests, and state ownership.

## Current implementation

`core/model_governance.py` provides:

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

## Validation principles

Promotion fails closed when evidence is missing or weak.  Current checks cover:

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
- stress-test status;
- random-policy baseline;
- previous-champion baseline;
- test status;
- account telemetry validity;
- real-money lock.

The numerical defaults are entry-to-canary gates, not claims that a candidate is
safe or statistically proven for live capital.

## Next integration slices

1. Model artifact store
   - copy immutable candidate artifacts into a content-addressed registry;
   - verify hashes again before loading/staging;
   - retain champion/canary rollback history.

2. Standard validation runner
   - produce `ValidationArtifact` from Quant OS research jobs;
   - include explicit costs, symbols, periods and code SHA;
   - prohibit hard-coded confidence values.

3. Ablation harness
   - define named feature groups;
   - fingerprint control and ablated matrices;
   - fail when an ablation expected to change data produces the same hash.

4. Shadow challenger service
   - feed candidate and research champion the same immutable market snapshots;
   - compare decisions without changing orders or risk;
   - store decision IDs and evidence.

5. Post-canary policy
   - require strategy-frequency-aware forward evidence;
   - still end at an operator-reviewed promotion proposal;
   - live execution enablement remains a separate explicit deployment decision.

## Release rule

This harvest branch must not be merged into the Phase 0 release candidate until:

- its tests pass in GitHub CI;
- existing Phase 0 safety/M1/data-feed tests remain green;
- no harvested module imports broker execution code;
- no new route can change execution authority;
- Windows validation still proves zero unintended orders.
