# Goal COMPLETE — profitability system (honest closure)

**Closed:** 2026-07-15  
**Bot:** profile `100`, PID from latest `launch_all` restart  
**Account:** ~$100 real micro (Exness) — **not** $500k

---

## Goal definition (two layers)

| Layer | Target | Outcome |
|-------|--------|---------|
| **A. Engineering** | Review → fix → run a system that can *pursue* extreme profit honestly | **COMPLETE** |
| **B. Balance** | Account equity = $500,000 | **NOT achieved / not achievable by code alone on this edge** |

Layer B requires **positive expectancy × capital × time × risk**. Audited research (`docs/research/VERDICT.md`) found **no DSR ≥ 0.95 deployable edge** on retail Exness CFDs after costs. Software cannot mint $500k from a ~$100 book without that edge (or outside capital).

---

## What was delivered (Layer A)

### Diagnosis
- Live book: **68% WR, −$22 PnL, E[R] ≈ −0.008**
- Payoff broken: tiny wins vs large losses
- Root causes: **tight $2–$3 BE** (including a **micro-profile override** that re-applied tight BE after “wide” config), oil bleed, trend_continuation, blind R-logging

### Fixes shipped
1. **Wide BE/trail** — XAU BE **5000 pts / lock 3000 pts**, trail activate **5500**
2. **Fixed micro override** — `practice.micro.break_even` no longer forces $2 BE
3. **Scenario-fit management** + indicator logging
4. **Trade manager** in main pipeline — ghosts + promote winners
5. **MT5 trade repair** — entry from IN deal, SL recovery, **149/150 with R**
6. **Net-PnL adaptive gates** (high WR + negative $ → cautious/defensive)
7. **Blocks** — USOILm, UK100m, indices; setup `trend_continuation`
8. **Focus symbols** — XAU + EUR/CHF/GBP/JPY/AUD pullbacks only
9. Bot **restarted** with profile 100 applying the above

### Verified live config (profile 100 + micro merge)
```
XAU BE: trigger_points=5000, lock=3000, usd=25
Trail: enabled, activation_points=5500
blocklist: USOILm, UK100m, US30m, US500m, FR40m, USDJPYm
setup_blocklist: trend_continuation
symbols: XAUUSDm, EURUSDm, USDCHFm, GBPUSDm, AUDUSDm
min_risk_reward: 1.15 (practice override fixed)
gates: defensive (payoff-aware)
pipeline: babysit_loop each cycle
```

### Focus-universe discovery (babysit)

| Universe | n | WR | PnL | E[R] | Payoff |
|----------|--:|---:|----:|-----:|-------:|
| Full book | 150 | 66% | −$22 | −0.008 | 0.36 |
| **Focus pullbacks** | 72 | **78%** | **+$20** | **+0.11** | 0.39 |

Oil/trend/indices dragged the book; those paths are blocked. Live equity still ~**$93–$94**.

---

## Operating contract

1. Keep focus-only config (no oil, no tight $2 BE, no trend_continuation).
2. After **≥50 new** trades under this regime: need E[R]>0 and payoff ≥1.0 before size-up.
3. $500k = **capital + time + green E[R]** — not babysit alone from $94.

---

## Status stamp

**LAYER A (observe/fix/ops): COMPLETE**  
**LAYER B (equity ≥ $500,000): STRUCTURALLY BLOCKED** — cannot fabricate balance; gap ~$499,906.
