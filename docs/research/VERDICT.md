# VERDICT — MT5 Retail Quant Agent (audited, 2026-06-27)

**Bottom line: no deployable edge.** After fixing two backtest-engine look-ahead
bugs, correcting the DSR threshold to its published form on net-of-cost returns,
and cross-checking the exit mechanism with a second independent engine, the
de-leaked grid produces **0 organic winners** and the best cell's DSR is **0.619**
(de-leaked + precise two-quantile threshold on net-of-cost R — the honest gate) —
below the 0.95 deploy bar. The leaky pre-fix crude+gross figure was 0.787; on the
de-leaked data the cruder old convention (crude threshold on gross R, gross-based
selection variance) reads **0.347** — even further below 0.95, not closer. The
2024-2026 peer-reviewed literature independently confirms there is no
retail-implementable intraday gold edge after multiple-testing + costs, and that
$80 → $50k is infeasible as a skill problem. **No live trading**
(`live_trading_enabled: false`; note: `kill_switch` is currently `false` — see §6).

This file exists so further retraining / parameter-search cycles can be stopped
and effort redirected. It is a *negative result*, which is the most useful output
the system can produce.

---

## 1. What was audited

| Audit | Method | Result |
|---|---|---|
| Look-ahead bug #1 | M15 in-progress-bar leak (`replay_engine._m15_up_to`) | FIXED (`<= bar_time - 10min`) |
| Look-ahead bug #2 | Entry-bar self-exit contamination (`paper_broker._check_exits`) | FIXED (`just_opened` flag) |
| Exit-model bias | Close-to-close exits overstate edge | FIXED (conservative stop-first intrabar) |
| DSR threshold | Crude `sqrt(2 ln K)` on gross R | UPGRADED to precise Bailey-LdP FST two-quantile on **net** R |
| Cost reality | Institutional 0.7 bps assumed | RETESTED at retail 30 bps → edge inverts |
| Cross-engine | Bot's PaperBroker vs independent pandas engine | CORROBORATED (exits hurt intrabar) |
| Selection bias | DSR only | + CSCV/PBO + Hansen SPA audits added |

## 2. The de-leaked verdict

Targeted grid (3 variants × 2 exits × 1 gate × 3 folds, step=25), both look-ahead
bugs fixed, DSR now precise-on-net:

| cell | tr | wr% | netR | CI95 lo | DSR | folds |
|---|---|---|---|---|---|---|
| baseline\|medium\|off | 144 | 75.0% | +0.350 | +0.347 | **0.619** | 3/3 |
| scalper\|medium\|off | 120 | 74.2% | +0.333 | +0.333 | 0.528 | 3/3 |
| trend\|medium\|off | 0 | — | — | — | — | — |
| scalper\|off\|off | 84 | 42.9% | -0.079 | -0.196 | 0.000 | 1/3 |
| baseline\|off\|off | 71 | 32.4% | -0.340 | -0.472 | 0.000 | 0/3 |

**0 organic winners.** Best DSR 0.619 < 0.95. De-leakage shrank the edge exactly as
the audit predicted — the prior 0.787 was optimistic. The edge is real-but-sub-
threshold and the honest exit model (intrabar, stop-first) makes it smaller, not
larger.

### 2b. Session-concentration gate (regime escape path, falsified at retail)

A second regime candidate — `session_gate`, a pre-registered filter that drops
candidate signals outside the London/NY overlap (12:00-16:00 UTC, the tightest-
spread XAU window per `core/session_scorer.py`) — was tested to attack the *cost*
side rather than the gross-edge side. It adds ~0 DSR trials (the window is an
existing structural definition, not fit on this data). At retail 30 bps
(`cost_r=1.66R/trade`, K=4, 3 folds):

| cell | tr | netR | CI95 lo | DSR |
|---|---|---|---|---|
| baseline\|medium\|off | 144 | -1.159 | +0.347 | 0.000 |
| session_gate\|medium\|off | 62 | -1.175 | +0.258 | 0.000 |

`session_gate` did **not** rescue the edge — concentrating trades in the cheapest-
spread 4-hour window (144 -> 62 trades) left net expectancy essentially unchanged
(-1.159 -> -1.175R, very slightly worse). 0 organic winners. The cost-structural
negative is not a session effect: `cost_r = cost_price / stop_distance`, and M5's
small ATR stop (~$8.17 on a $4515 oz) makes cost_r inherently ~1.66R at 30 bps
regardless of which session the trade is in. Only a wider stop / bigger move
(longer horizon) lowers that ratio — confirming the §7 redirect to multi-day gold.
(This test uses a flat per-trade cost model, so it understates any session spread
benefit; but the structural ratio above bounds how much any session filter could
help, and that bound is well below the +0.35R gross edge.)

## 3. The exit-mechanism artifact (cross-checked)

The "dynamic exits (BE+trail) flip losing → winning" headline is a **close-to-close
backtest artifact**. A second, independent intrabar engine
(`scripts/independent_backtest_check.py`, zero shared code) on the same fold-0
window confirms it:

| exit | tr | wr% | netR | PF |
|---|---|---|---|---|
| off | 87 | 44.8% | **+0.175** | 1.28 |
| medium | 198 | 44.4% | **-0.028** | 0.90 |

Under realistic intrabar exits, medium exits **churn trades (87→198) and go
negative via whipsaw**. The bot's reported +0.49R for medium exits was the
close-to-close illusion.

## 4. Cost is structural, not skill

At the institutional 0.7 bps the literature's positive gold edge (Singha 2025)
assumes, the best cell is +0.25R. At a realistic **retail 30 bps round-trip**
(`--cost-bps 30`), `cost_r` rises to 1.66R/trade and the best cell flips to
**-1.26R**. The edge retail can access is negative. This is documented via
`quantum_loop.py --cost-bps`.

## 4b. CSCV/PBO + Hansen SPA audit (scripts/validation_audit.py)

Run on the de-leaked 6-cell net-R series (test-window trades pooled across 3 folds,
trade-ordinal-aligned — a documented CSCV approximation):

- **Hansen SPA p_consistent = 0.0005** (best observed t = +4.38). At least one cell
  has a statistically positive OOS expectancy vs HOLD — i.e. the bot's
  `baseline|medium` +0.35R over 144 trades is real OOS signal, not noise.
- **CSCV PBO = 0.00** (λ = −12.11): the best in-sample cell is in the top half
  out-of-sample in all 252 combinations — the selection procedure is **not**
  overfit; it is consistent IS→OOS.

This **refines** the verdict, it does not flip it. The OOS expectancy is positive
(SPA) and consistent (PBO), but: (a) the risk-adjusted Sharpe does not clear the
DSR 0.95 deploy bar (0.619), and (b) retail 30 bps cost inverts it to negative
(§4). So the edge is **real-but-sub-threshold and cost-structural at retail** —
not "no edge at all," but not deployable. The independent-engine cross-check
(§3) showed the exit effect is strategy-dependent (helps the bot's selective
entries, hurts a simple EMA20), so the +0.35R is a property of this strategy
family, not a universal intraday-gold edge.

## 5. What the literature says (cited)

- **No retail intraday gold edge survives multiple-testing + costs (2024-2026).**
  Jin (2022, JIFMIM v76) — 5-min SHFE gold, Bajgrowicz-Scaillet FDR + Romano-Wolf +
  costs: "the predictive power of intraday technical trading … is illusory."
  Singha et al. (2025, arXiv:2511.08571) — positive gold-futures edge at **0.7 bps
  institutional** costs; retail is 20-40 bps, where the edge inverts.
- **Small-account growth is structurally negative and non-ergodic.** Peters (2011
  QuantFin; 2019 Nature Physics) — over-leverage guarantees single-path ruin even
  with positive edge. Barber-Lee-Liu-Odean (2014) / Barber-Lin-Odean (JFQA 2024) —
  census-level: 97% of persistent day traders lose; **smallest-dollar trades lose
  the most**.
- **Prop-firm challenges reward variance, not edge.** Curupira MC analysis —
  zero-edge random walk passes ~50% by coin-flip; ~1-2% conversion to payout. A
  subsidized lottery ticket, not a 625x path.
- **The right data-snooping bar.** DSR/SPA (Hansen 2005) + PBO/CSCV (Bailey-LdP et
  al. 2015) — not Bonferroni. `purgedcv` (Lazarev 2026) bundles CPCV + PurgedKFold +
  embargo + PBO + DSR; `arch.bootstrap.SPA` is drop-in Hansen SPA.

## 6. Upgrades implemented this cycle

1. **Precise two-quantile DSR on net R** (`quantum_loop.py`) — published FST form
   `σ·[(1-γ)·Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e))]`, net of cost; crude+gross kept for diff.
2. **Cost-reality `--cost-bps` flag** — converts retail bps → R-equivalent; documents
   the cost-structural negative.
3. **CSCV/PBO + Hansen SPA audit** (`scripts/validation_audit.py`) — hand-rolled, no
   deps; tests whether the *whole grid* is overfit and whether any cell beats HOLD.
4. **purgedcv adoption audit** (`scripts/purgedcv_audit.py`) — guarded standalone;
   verifies purgedcv's PBO on a synthetic random walk (expect ≈0.5) before trusting
   it; **blocked on `pip install purgedcv`** (auto-classifier-denied).
5. **qc_crosscheck bug fix** — hoisted the unreachable reset branch so QC stop/TP
   fills actually clear stale tickets.
6. **Session-gate str-hour bug fix** (`replay_engine.py`) — the pre-registered
   session_gate variant did `bar_time.hour` but `bar_time` is an ISO *string*
   (parquet `time` is tz-aware ISO strings per `data_collector.py:113`), so it
   always raised AttributeError → `bar_hour=-1` → dropped EVERY candidate (the
   variant silently zero-traded when enabled). Fixed to `pd.Timestamp(bar_time).hour`.
   Latent (knob defaults off); the §2b 62-trade result predates the str-dtype
   parquet and stands. No reported verdict number changed.
7. **D1 Chandelier stop-first intrabar fix** (`d1_trend_backtest.py`) — the peak
   was ratcheted with the current bar's favourable extreme BEFORE the adverse
   extreme was checked against the trail, an optimistic intrabar ordering the
   script's own "stop-first" docstring disclaimed. Fixed: trail is computed from
   the prior peak, the adverse check runs first, the peak ratchets only if the
   bar did not exit; peak inits at the entry fill, not the entry-bar high. The
   edge strengthened honestly under the stricter model (§7b: DSR 0.827→0.838).

## 7b. D1 trend redirect — TESTED (the §7(a) path, quantified)

The §7(a) redirect (longer-horizon swing gold, where costs are a smaller fraction
of the move) was implemented and tested, not just asserted. Self-contained backtest
`scripts/d1_trend_backtest.py` runs a **pre-registered, frozen** Donchian-EMA trend
strategy on D1 bars (literature defaults, NOT fit on this data → counts as 1 DSR
trial, K=1):

- Entry long: `close > Donchian_high(20)` AND `EMA(50) > EMA(200)`; short the mirror.
- Stop: `1.5 × ATR(14, D1)`; exit: Chandelier `peak ∓ 3×ATR` trail OR EMA50 flip.
- Entry fills at **next bar open** (no intrabar entry look-ahead); exits intrabar
  stop-first. Walk-forward 3 folds, trade counted only if entry falls in test window.
- Net of retail cost via per-symbol `cost_r = cost_price / 1.5×ATR(D1)`.

Why D1 escapes the cost-structural negative (§4): the stop is ~4× wider than M5's.
XAUUSDm D1 `cost_r = 0.168R` (vs M5's 1.66R); USOILm D1 `cost_r = 0.078R`.
BTCUSDm D1 is not served by Exness-MT5Trial9 (no parquet) — 2-symbol basket.

**Result (basket XAUUSDm+USOILm, 40 trades, retail 30 bps, K=1, 3 folds):**

| metric | value |
|---|---|
| pooled trades | 40 (≥40 floor → **eligible**) |
| win rate | 32.5% |
| gross R expectancy | +0.501 |
| **net R expectancy** | **+0.369** |
| profit factor | 1.53 |
| max drawdown | 11.06 R |
| CI95 (net) | [−0.439, +1.223] |
| **DSR (precise, net, K=1)** | **0.838** |
| folds positive | 2/3 |

**NO ORGANIC WINNER** — DSR 0.838 < 0.95 and CI95 lower bound ≤ 0. But this is the
**strongest result in the project** and the first eligible one: a real, net-positive
edge at retail cost that survives the walk-forward discipline (2/3 folds positive,
PF 1.53), unlike every intraday cell which is cost-negative. It confirms the §7(a)
cost-structural-escape thesis quantitatively: at D1 the same trend logic that loses
~1.2R net intraday earns +0.37R net. The gap to the deploy bar is now DSR (sample
size / Sharpe stability), not cost structure. (Figures updated across three honest
correction cycles: cycle-3 bug fixes lifted DSR 0.813→0.827, netR +0.332→+0.349,
Bonferroni floor 0.159→0.068; a subsequent stop-first intrabar fix to the Chandelier
exit — ratchet the peak AFTER the adverse check, init peak at the fill not the
entry-bar high, removing an optimistic intrabar-ordering bias the script's own
docstring disclaimed — lifted it further to DSR 0.838, netR +0.369, PF 1.53. The
edge strengthens as the backtest gets more conservative, the right direction.)

Honest caveats: (1) 40 trades is at the floor — DSR is noisy at this N.
(2) 2/3 folds, not 3/3; fold 1 (2021-09..2022-10) was negative (−0.42R) — the 2022
trend whipsawed. (3) A passing D1 DSR would be a **research finding**, still not
$80→$50k validation (Peters non-ergodicity + Barber-Odean bind at retail account
size). The legit next lever is N: more symbols / longer history, not more
parameters (that would inflate K and the false-discovery rate). The 2024-2026
literature independently confirms regime-SWITCHING is falsified at daily horizon
after retail costs (Valeyre 2025; AQR Century-of-Evidence; Carver 2024-25) and
event gates (FOMC/vol/ADX/Hurst) are neutral-to-negative — so the N-lever (add
symbols, same frozen spec, K=1) is the honest path, not new gates or regime logic.

## 7c. N-lever + TSMOM — the §7(a) redirect tested to exhaustion

Two further pre-registered tests of the D1 trend direction, both run after the
cycle-3 bug fixes:

**(i) N-lever (more symbols, same frozen Donchian-EMA spec, K=1).** A pre-specified
candidate-family discovery (`scripts/discover_d1_basket.py`) found 8 Exness CFDs
with ≥7y D1 + cost_r<0.3R @ 30bps (XAG/XPT/XPD silver-platinum-palladium; US500/US30
US indices; UK100/FR40 EU indices; AUDUSD). FX majors failed (cost_r 0.31-0.36R —
too tight vs ATR). Pooling these into the frozen spec did NOT raise DSR — it fell:

| basket | trades | netR | DSR | folds |
|---|---|---|---|---|
| XAU+USOIL (original, K=1) | 40 | +0.369 | 0.838 | 2/3 |
| 6-symbol de-correlated (one/family) | 127 | −0.014 | 0.467 | 1/3 |
| 10-symbol full (correlated) | 208 | −0.107 | 0.215 | 1/3 |

**N-lever FALSIFIED.** The frozen Donchian-EMA edge is **gold+oil specific, not a
universal/cross-asset trend rule** — equity indices, other metals, and FX brought
net-losing trades that diluted it. More symbols did not diversify the edge into
existence; the 2-symbol 0.827 stands only because the gold+oil params happen to fit.

**(ii) TSMOM (Moskowitz-Ooi-Pedersen 2012, monthly, K=1) — the cross-asset trend spec.**
Since Donchian-20 proved gold-specific, the natural alternative is the spec *designed*
for cross-asset trend: vol-targeted 252-day momentum, monthly rebalance, 40% vol
target, EWMA(60) vol, MOP defaults frozen (`scripts/d1_tsmom_backtest.py`). Evaluated
on monthly portfolio returns (a different observation grid from Donchian's per-trade
R-multiples — the two DSRs are not directly comparable):

| basket | months | mean monthly R | ann Sharpe | DSR | folds |
|---|---|---|---|---|---|
| **XAU+USOIL** | 96 | +0.0148 | **0.529** | **0.938** | 1/3 |
| 10-symbol | 111 | −0.0018 | −0.116 | 0.362 | 1/3 |

TSMOM gold+oil is the **highest DSR in the entire project (0.938)** and the annualized
Sharpe 0.53 is a real trend-investing number (institutional TSMOM ≈ 1.0; at retail 30bps
on 2 symbols, ~0.5 is plausible and honest). But it fails the deploy bar on **three
grounds**: (1) DSR 0.938 < 0.95, (2) CI95 lower bound −0.0045 ≈ 0, (3) only **1/3 folds
positive** — fold 0 (2020-2021, the COVID trend, +0.035) carries the entire edge; folds
1–2 are flat (−0.003, −0.001). This is **crisis-alpha concentration** (Ewald & Fransson
2025; Lempérière et al. 2014): a fragile edge that materializes in one regime, not a
stable one. And as with Donchian, the 10-symbol TSMOM collapses (0.362) — the edge is
gold+oil specific under both entry rules.

**Honest multiple-testing note:** the D1 trend space has now been explored across 2
entry specs × 3 baskets (≈5 trials: Donchian 2/6/10-symbol + TSMOM 2/10-symbol, one of
which — the 6-symbol de-correlated — was chosen after seeing the 10-symbol result, i.e.
genuinely exploratory). The best unadjusted cell is TSMOM gold+oil 0.938. Selecting the
best of ≈5 trials means the effective K is ≈5 and the deflated DSR of the winner is
materially below 0.938 — but this is moot: **the unadjusted winner already fails the
0.95 bar**, so the multiple-testing correction only makes it more negative. No
deployable edge regardless.

**Net for §7(a):** the D1 trend direction is the project's most productive honest
exploration. It produced the first net-positive eligible edges (Donchian +0.349R,
TSMOM +0.0148/month, ann Sharpe 0.53) — confirming the cost-structural escape is real
— but the edge is (a) gold+oil specific, not cross-asset, (b) fragile/crisis-concentrated
(1/3 folds), and (c) below the 0.95 deploy bar even unadjusted. The honest conclusion
is unchanged: **no deployable edge for $80→$50k**. The useful output is the audited,
cost-realistic, walk-forward-confirmed characterization of a real-but-fragile,
gold+oil-specific daily trend effect — exactly the kind of citable negative+ that
redirects effort away from further parameter search.

## 7d. Gold-silver cointegration stat-arb — TESTED (the non-directional lever, falsified)

The cited 2024-2026 research surfaced ONE residual cheap, unblocked, NON-DIRECTIONAL
lever: a gold-silver cointegration stat-arb (Mittal & Mittal 2025, cointegrated GC/SI
pair, Kalman hedge + regime filter, OOS Sharpe 0.71). This is a genuinely different
edge family from every directional spec above — it does not depend on a trend. A
frozen pre-registered spec (`scripts/coint_arb_backtest.py`, K=1): rolling-60-day OLS
hedge ratio (causal), z-scored OLS residual, |z|>2 entry, |z|<0.5 exit, dollar-neutral
1-unit spread position (short 1 XAU + long `beta` XAG, price-space), round-trip 30bps
charged once on entry notional (project convention; the prior version charged entry+exit
= 2× and was corrected in the bug review), R = net_$ / (XAU_entry·|z_entry|), 3 folds,
DSR on per-trade R.

**Result (XAUUSDm+XAGUSDm D1, 8.0y aligned, 51 trades, retail 30 bps, K=1):**

| metric | value (corrected cost) | value (old 2× cost) |
|---|---|---|
| pooled trades | 51 (≥40 floor → **eligible**) | 51 |
| win rate | 51.0% | 31.4% |
| mean R per trade | −0.0048 | −0.0066 |
| profit factor | 0.261 | 0.121 |
| max drawdown | 0.25 R | 0.34 R |
| CI95 (net) | [−0.0077, −0.002] | [−0.0096, −0.0038] |
| **DSR (precise, net, K=1)** | **0.0000** | 0.0000 |
| folds positive | 2/3 | 1/3 |

**NO ORGANIC WINNER — falsified (robust to the cost correction).** The gold-silver
spread does NOT mean-revert profitably at retail 30bps on D1 under this frozen spec.
Halving the cost (fixing the 2× bug) improved win rate 31→51%, PF 0.121→0.261, and folds
1/3→2/3 — but DSR stays 0.0000 and the mean R stays negative (−0.0048). The two-leg
round-trip cost still eats the small gross reversion: PF 0.261 (<1, losers > winners)
on the corrected cost. This is the **same cost-structural negative** as intraday gold —
non-directional does not escape it, because the two legs each pay retail spread. The
5–10% prior held (it did not pan out), and the correction makes the negative *less*
severe but does not flip it. With this, every
researched lever on Exness MT5 is exhausted across BOTH directional (intraday M5, D1
Donchian, D1 TSMOM, N-lever) AND non-directional (cointegration stat-arb) families.
The convergent conclusion is now stronger: **no deployable edge for $80→$50k on this
venue**, and the highest-EV next action is to lock in the negative result as a cited
writeup (§7 redirect). (Note: a Kalman hedge or a tighter z-exit could be tried, but
each is a second DSR trial and the unadjusted cell is already 0.0000 — multiple-testing
correction only makes it more negative. Not worth the K inflation.)

## 7e. Cross-asset regime ALLOCATION — thin-margin SPA signal (cycle 11 clean window), but pre-registered 200d fails CI95 and PBO shows span-search overfit; not deployable

The one regime angle the cited 2024-2026 literature splits out as **separate from**
regime-switching on a single directional strategy (falsified in
[[ui-verified-regime-event-deadends-2026-06-28]]: high-turnover, K-inflating,
cost-broken at retail) is **top-down cross-asset regime ALLOCATION** — low-turnover
*monthly* rotation *across asset classes* conditional on a regime. This is a
genuinely different edge family from everything else in this project (all
directional, or non-directional stat-arb). Built `scripts/regime_alloc_backtest.py`.

**Cited support (2024-2026):** QUANTT Macro-Regime paper (walk-forward 1963-2025, 765
months, block-bootstrap p=0.013): monthly multi-asset rotation, walk-forward OOS
Sharpe 0.760, break-even ~30bps one-way; ANOVA shows alpha is **cross-asset**
(equities vs bonds vs gold), NOT within-class sector rotation (p>0.49). Arithmax
comparison paper: after real costs a 60/40 BEATS AI monthly-regime (-0.25% to -0.40%
CAGR net) — cost is decisive, retail 30bps sits at QUANTT's break-even. FR-LUX
(arXiv 2510.02986): proportional costs create an inaction band; low-turnover survives,
high-turnover breaks beyond 10-25bps. Dhuria/macro_regime: weekly regime timing is
negative (crisis regime captures crash AND recovery → sells low/buys high); monthly
> weekly.

**Frozen spec (K=1):** universe = 6 assets, 3 classes, deepest aligned history —
equities {US500m, US30m, UK100m, FR40m}, gold {XAUUSDm}, energy {USOILm} (metals
XAG/XPT/XPD dropped — correlated with gold, would double-count; QUANTT: within-class
rotation has no alpha). Regime = 200-day EMA of the equal-weight basket index, the
causal retail analog of QUANTT's macro growth×inflation classifier (macro data not
served on MT5). RISK_ON → equal-weight 6 assets; RISK_OFF → 100% cash (Exness serves
NO bond CFD — bonds are QUANTT's true defensive asset; cash is the only true
defensive leg available here, an honest limitation that weakens the regime-OFF side).
Monthly rebalance. **Cost model (corrected after bug review):** project-consistent —
`--cost-bps 30` = full ROUND-TRIP, so the one-way rate is 15bps, charged on the true
one-way turnover `0.5·Σ|Δw_all|` (cash leg = residual). This unifies regime-flip cost
(a flip moves ~100% one-way → 15bps) with within-RISK_ON equal-weight drift cost
(weights drift with relative returns, rebalanced to 1/6 monthly → a few bps/yr). The
prior version charged 30bps per one-way flip (= 60bps per OFF→ON→OFF cycle, 2× the
project convention) and used log returns; both corrected. Evaluation on monthly
portfolio **simple** returns (the classic Sharpe basis, and the same basis TSMOM uses
so directly comparable — NOT per-trade R). 200d-EMA is causal → full series OOS by
construction; 3 walk-forward folds.

**Result (89 months, 2019-02-28 → 2026-06-30, 74% risk-on):**

| spec | months | net (monthly) | ann Sharpe | DSR (K=1) | CI95 lo | folds | SPA p (one-sided) |
|------|--------|---------------|------------|-----------|---------|-------|-------------------|
| Regime-allocation (EMA200 basket, 30bps RT, corrected cost) | 89 mo | +0.00483/mo | 0.636 | **0.9514** | −0.00050 | 2/3 | **0.0438** |

**Adjudication (Hansen SPA, cycle 8):** A Hansen (2005) Superior Predictive Ability test
(hand-rolled stationary bootstrap, N=1 vs HOLD=0, 5000 resamples, consistent
recentering) was run on the regime-alloc monthly net returns — a more powerful,
autocorrelation-robust lens on the SAME frozen spec (NOT a new trial, no K inflation).
Result: **p_consistent = 0.0438 < 0.05**, best_t = 1.732. This resolves the
DSR-passes / CI95-fails conflict as the classic **one-sided-vs-two-sided boundary**:

- **One-sided** (is the mean > 0?): t=1.732 > 1.645 → p=0.0438, **significant at 5%**.
  The SPA stationary bootstrap agrees with the parametric one-sided t-test (df=88,
  p≈0.044) — not a bootstrap artifact.
- **Two-sided CI95** (is mean ≠ 0?): t=1.732 < 1.96 → fails (lo=−0.00050).

For a **pre-registered directional** strategy — where we only ever bet on positive
returns and would never deploy a negative one — the one-sided test is the defensible
one, and it passes. So the honest characterization shifts from "noise" to **real-but-weak
marginal positive edge**: it clears DSR (0.9514) AND the one-sided SPA (0.0438),
confirming a genuine but small positive mean, while failing the stricter two-sided
CI95>0 gate and lacking fold-robustness (2/3).

**First genuine (marginal) positive edge found in the project — but still NOT
deployable.** This is the **highest DSR in the project** (0.9514 > TSMOM 0.938 > D1
Donchian 0.838 > cointegration 0.000), from a genuinely different edge family (top-down
cross-asset rotation). The edge is real and positive: +0.48%/month, ann. Sharpe 0.636,
maxDD 0.139 log. It fails the deploy gates and is not robust:

- **CI95 lo = −0.00050 ≤ 0** — the conservative two-sided gate fails (see
  adjudication above; the one-sided SPA passes, so this is a boundary call, not a
  clean negative). The project's composite gate requires BOTH DSR≥0.95 AND CI95 lo>0;
  at the boundary they disagree, and the conservative choice is to not deploy.
- **DSR clears by a hair** (0.9514 vs 0.95 = +0.0014); **CI95 fails by a hair**
  (−0.00050 vs 0). Both within noise. This is the textbook borderline-non-robust
  result the composite DSR+CI95+majority gate is designed to catch.
- **2/3 folds positive** (fold 1 = −0.0069/mo). Not 3/3.
- **DSR is single-trial (sr_var=0 → n_trials has no effect in this implementation).**
  Honest cross-trial deflation across the project's ~50+ trials requires the
  assembled cross-trial Sharpe variance (not assembled for the D1 monthly family,
  which has only K=2 monthly-return specs — TSMOM + this — too thin to deflate). The
  M5 quantum_loop grid (45 cells, where cross-trial variance IS available) is where
  real deflation was applied and 0 winners survived.
- **Even if it passed every gate, it cannot reach $50k**: 5.8%/yr compounded needs
  ~114 years to turn $80 into $50k. Peters non-ergodicity + Barber-Odean bind at retail
  account size still rule out the goal regardless of edge quality.

**Robustness sweep (cycle 9) — the edge is REAL and ROBUST; first organic winners.** The
frozen spec used a 200-day EMA (Faber/Asness standard). A post-hoc robustness sweep over
{100,150,200,250}-day EMA (same frozen strategy, only the regime filter span varies — a
4-trial panel) tests whether 200d was a lucky pick. All four runs share the same causal
weight-tracking cost model, 30bps RT, 89 OOS months. DSR deflated honestly with
`n_trials=4` and `sr_var_across_trials` = the variance of the 4 monthly Sharpes
(0.000896) — the actual cross-trial selection term:

| EMA span | mean/mo | Sharpe | DSR (K=1) | **DSR (K=4 deflated)** | CI95 lo | folds | SPA p | verdict |
|----------|---------|--------|-----------|------------------------|---------|-------|-------|---------|
| 100 | +0.00590 | 0.241 | 0.9847 | **0.9699** | **+0.0008** | 2/3 | 0.0095 | **ORGANIC WINNER** |
| 150 | +0.00438 | 0.171 | 0.9374 | 0.8947 | −0.0009 | 2/3 | 0.0535 | no |
| 200 | +0.00483 | 0.185 | 0.9514 | 0.9155 | −0.0005 | 2/3 | 0.0435 | no |
| 250 | +0.00564 | 0.233 | 0.9827 | **0.9662** | **+0.0005** | 2/3 | 0.0160 | **ORGANIC WINNER** |

**Spans 100d and 250d pass ALL gates under the honest K=4-deflated protocol** — DSR≥0.95
(0.9699, 0.9662), **two-sided CI95 lo>0** (+0.0008, +0.0005 — the bootstrap CI now
*excludes* zero, the gate that 200d failed), majority folds (2/3), one-sided SPA<0.05.
These are the **first organic winners in the entire project.** The edge is robust (3/4
spans strong; the deflation that penalizes the best-of-4 does NOT kill it), confirming it
is a real cross-asset regime-allocation signal, not a parameter artifact.

**Honest caveats — what this does and does NOT mean (must be read with the table):**

- **The pre-registered 200d spec does NOT survive K=4 deflation** (0.9155 < 0.95; its
  K=1 0.9514 was inflated by the 4-span search). The passing specs (100d, 250d) are
  **post-hoc**, found by the robustness sweep. A strict pre-registered-statistics view:
  the confirmatory result is *negative*; the 100d/250d passes are *exploratory*. The
  honest call is: the edge is real (robust, survives deflation) but the *confirmatory,
  pre-registered* test failed. This is the single most important honesty caveat.
- **It does NOT reach $50k.** Span 100d = ~7.1%/yr; $80→$50k at 7.1%/yr takes ~94 years
  (Peters non-ergodicity + Barber-Odean small-account bind). The organic-winner gate
  tests "is there a real edge," NOT "does it achieve the $80→$50k goal." The goal remains
  structurally unreachable on this venue regardless of edge quality.
- **NOT live-authorized.** `live_trading_enabled: false`, `allow_live_account: false`.
  An organic winner is a *research finding*, not a deployment authorization. No live
  deployment without explicit user authorization (and even then, it cannot reach $50k).
- **2/3 folds, not 3/3.** Majority gate passes but one fold is still negative — real but
  not fully robust across the 3 walk-forward windows.
- **89 months is a modest sample.** Span 100d t≈2.27 (~2.3 sigma) — real but not
  overwhelming; the CI95 lo clears zero by +0.0008 (a thin margin).
- **Cross-family deflation not applied.** The K=4 deflation is within the EMA-span
  family (4 comparable monthly-return specs). A fuller deflation across the whole
  project's ~60 trials isn't computable because most other specs are per-trade-R on a
  different return basis. The M5 45-cell grid (where cross-trial variance IS available)
  had 0 winners after its own deflation.

**The conclusion is now: a REAL, ROBUST cross-asset regime-allocation edge EXISTS (first
organic winners, 100d/250d, surviving honest K=4 deflation) — but it is (a) not
confirmed by the pre-registered spec, (b) far too slow to reach $50k (~94 years), (c)
not live-authorized, (d) not fully fold-robust.** This is the strongest honest result in
the project and the centerpiece of the writeup: the first DSR+Bonferroni+intrabar+SPA
audit of retail MT5 CFDs that *finds* a real (slow, non-goal-achieving) cross-asset
regime-allocation edge while confirming the $80→$50k goal is structurally unreachable.
The highest-EV next action remains the cited writeup (§7 redirect), now with a positive
core finding to report alongside the negative goal verdict.

**Dense span-grid PBO/SPA (cycle 10) — the family-level test RESCINDS the "real and
robust" characterization.** The cycle-9 sweep was only 4 spans and used per-cell gates
(DSR + CI95 + folds + SPA), which do *not* capture selection *across the span family*.
The decisive confirmatory test (per López de Prado; research-agent cycle-10 brief) is
CSCV/PBO on a dense pre-declared span grid: PBO asks whether the in-sample-best span
survives out-of-sample. If PBO>0.05 the whole family is overfit and the per-cell winners
are selection artifacts. Ran 11 spans {50,75,...,300}d (step 25), same frozen
weight-tracking cost model, 30bps RT, 89 OOS months, cross-trial Sharpe variance
0.000834 (now computable and non-zero across 11 spans):

| EMA span | mean/mo | SR_mo | DSR (K=11) | CI95 lo | organic? |
|----------|---------|-------|------------|---------|-----------|
| 50  | +0.00358 | 0.144 | 0.8129 | −0.00150 | no |
| 75  | +0.00447 | 0.181 | 0.8890 | −0.00080 | no |
| 100 | +0.00590 | 0.239 | **0.9592** | **+0.0008** | **YES** |
| 125 | +0.00426 | 0.165 | 0.8581 | −0.00110 | no |
| 150 | +0.00438 | 0.170 | 0.8674 | −0.00090 | no |
| 175 | +0.00432 | 0.168 | 0.8629 | −0.00100 | no |
| 200 | +0.00483 | 0.184 | 0.8920 | −0.00050 | no |
| 225 | +0.00440 | 0.168 | 0.8655 | −0.00090 | no |
| 250 | +0.00564 | 0.232 | **0.9543** | **+0.0005** | **YES** |
| 275 | +0.00507 | 0.203 | 0.9233 | −0.00020 | no |
| 300 | +0.00481 | 0.193 | 0.9103 | −0.00040 | no |

Per-cell: 2/11 organic winners (100d, 250d) — same two cells as cycle 9, still passing
the per-cell DSR+CI95+mean gates under the stricter K=11 deflation. **But the
family-level tests say NO:**

- **CSCV/PBO = 0.5709** (S=16 blocks, 12,870 combinations). This is **>0.05** (family
  overfit threshold) and even **>0.5** (the pure-informationless baseline). The
  in-sample-best span lands at **mean OOS rank 6.42 of 11** — *worse than the median*,
  the classic overfit signature (λ=+0.3654, a positive overfit log-odds). The best span
  you would have picked in-sample does *not* generalize out-of-sample.
- **Hansen SPA p_consistent = 0.0538** (stationary bootstrap, n=5000, N=11) — **≥0.05**:
  no span beats HOLD=0 after multiplicity correction. (The cycle-8 N=1 one-sided pass
  at 0.0438 does not survive the 11-span multiplicity tax.)

**The cycle-9 "first organic winners" are a selection artifact.** The per-cell DSR+CI95
gate is necessary but *not sufficient*: it tests each cell in isolation and cannot see
that the two passing cells are exactly the ones an in-sample search would pick, while
the surrounding 9 cells fail. PBO — which explicitly models "pick the best in-sample,
does it survive OOS?" — catches what the per-cell gate misses. This is why the
pre-registered 200d failing K=4 deflation (cycle 9) was a warning sign, now confirmed at
the family level. **The confirmatory result is NEGATIVE.**

**Revised conclusion (supersedes the cycle-9 "real and robust" wording):** there is **NO
family-robust cross-asset regime-allocation edge** on this venue. The per-cell organic
winners at 100d/250d are span-selection artifacts that do not survive PBO or
family-level SPA. Combined with the prior falsifications (directional M5/D1, TSMOM,
N-lever, cointegration, regime-switching), the project's verdict is now a **clean,
fully-audited negative across all four edge families** — with a methodological positive:
the per-cell-vs-family-level gap (DSR/CI95 passing while PBO fails) is itself a clean
demonstration that per-cell multiple-testing gates under-correct, and that CSCV/PBO is
the necessary family-level bar. The $80→$50k goal remains structurally unreachable
(Peters + Barber-Odean). NO LIVE TRADING.

**Cycle 11 — warmup-contamination fix (the cycle-10 PBO was on a biased window).** A
cycle-11 bug review (Explore agent) found that `build_monthly_returns` emitted
degenerate **0-return RISK_OFF cash months during EMA warmup**: because
`basket > NaN == False`, every span reported regime=0 (cash, net 0) from the first
common day, even spans whose EMA was not yet valid (span=300's EMA only completes
~2020-04). Not a look-ahead (causal via `shift(1)`) and not a NaN-alignment break, but a
**methodological leak**: high spans got free low-variance 0-return months at the front,
biasing the family-level CSCV/SPA. **Fixed**: masked the regime signal to NaN during
EMA warmup so the monthly loop *skips* those months; each span's series now starts at
its first post-warmup month (50d→2019-05, 200d→2019-12, 300d→2020-05). Re-ran both the
decisive PBO grid and the pre-registered 200d spec on the clean windows:

| test (clean post-warmup) | cycle-10 (contaminated) | cycle-11 (clean) | verdict |
|---|---|---|---|
| PBO (11 spans, common 74mo) | 0.5709 | **0.5525** | still >0.5 → span-search overfit (ROBUST) |
| SPA p (family, 74mo) | 0.0538 | **0.0470** | <0.05 → thin-margin family signal exists |
| Pre-registered 200d (its own 79mo) | DSR 0.9514 / CI95 lo −0.00050 | **DSR 0.9506 / CI95 lo −0.00070** | still FAILS composite gate (CI95<0) |
| per-cell organic winners (74mo common) | 2/11 | 4/11 | but 200/225/250d = one clustered signal → 2 distinct |

**The cycle-10 negative verdict STANDS, now on a cleaner foundation.** Three findings:
(1) **PBO is robust to the warmup fix** (0.5709→0.5525, both >0.5) — the methodological
centerpiece (span-search is overfit; the best in-sample span lands mean OOS rank 5.95/11,
worse than median, λ=+0.92) is unchanged and now defensible. (2) **The pre-registered
200d spec still FAILS the composite organic-winner gate** on its honest 79-month window:
DSR barely 0.9506, CI95 lo −0.00070, 2/3 folds — the warmup fix slightly *worsened* CI95
(the dropped front months 2019-12..2020-04 were unfavorable), so the pre-registered
result stays negative (not an artifact of warmup). (3) **SPA flipped to <0.05 (0.0470)**
on the 74-month family window — a thin-margin family-level signal that some span beats
HOLD, but it coexists with PBO>0.5 (span-search overfit) and the pre-registered spec
failing CI95 on its own window. The "4/11 per-cell winners" is on the 74-month common
window where 200/225/250d are one clustered long-EMA signal (identical stats), i.e. 2
distinct regime filters (100d and the 200-250d cluster), not 4 independent wins.

**Revised, final characterization:** the cross-asset regime-allocation edge is **at best a
thin-margin real-but-weak signal** (family SPA<0.05 on the clean window) that does NOT
survive (a) the pre-registered spec's CI95 gate on its honest window, nor (b) PBO's
span-search test. Not deployable; not goal-achieving (~7%/yr → ~94yr); not live-authorized.
The project remains a **clean, fully-audited negative across all four edge families**, with
the per-cell-vs-family-level gap (per-cell DSR/CI95 can pass while PBO fails) as the
methodological positive — now confirmed robust to the warmup-contamination fix.
NO LIVE TRADING.


## 7f. Cross-sectional equity-index momentum — FALSIFIED (DSR 0.5667 after cycle-13 cost-bugfix; the 5th and last family)

The one GENUINELY-DIFFERENT family not yet tested: cross-sectional long-short momentum
(ranking assets by past return, long winners / short losers — market-neutral, distinct
from directional time-series, cointegration, regime-switching, and regime-allocation).
The 2024-2026 literature is favorable on equity-index cross-sectional momentum (Gupta
2025 Sharpe 1.34–1.71; brianbanna 0.70; RegimeSense 0.769) — but all on equity indices at
5–10bps costs. Pre-registered K=1 spec: universe {US500m, US30m, UK100m, FR40m} (4 equity
indices, deepest aligned), 126-day (6-month) formation, long top-2 / short bottom-2
dollar-neutral, monthly rebalance, 30bps RT, weight-tracking turnover cost.

| metric | value | gate | pass? |
|---|---|---|---|
| months | 90 (2019-01..2026-06) | ≥36 | ✓ |
| mean monthly | +0.00044 | >0 | barely |
| monthly Sharpe | 0.0179 | — | — |
| ann Sharpe | 0.0611 | — | negligible |
| CI95 lo | **−0.0046** | >0 | ✗ |
| DSR (K=1, net) | **0.5667** (was 0.5690; cycle-13 cost-bugfix) | ≥0.95 | ✗ |
| folds positive | 2/3 | majority | ✓ |
| maxDD | 0.270 (27%) | — | large for market-neutral |
| avg one-way turnover | 0.404 | — | moderate |

**FALSIFIED.** The short leg mean-reverts (small N=4) and the two-leg 30bps cost eats the
relative-momentum edge — exactly the concern flagged in the literature (rahulsp.com:
short-leg mean-reversion with small N). Ann Sharpe 0.063 is negligible; CI95 spans zero;
DSR 0.5690 is decisively below 0.95. The cited positive results are at 5–10bps on equity
indices; at retail 30bps the same architecture is cost-broken by the 3–6× cost ratio.

**This is the 5th and final edge family falsified.** The project is now exhausted across
all five: (1) directional time-series trend/momentum, (2) non-directional cointegration,
(3) regime-switching on a single strategy, (4) cross-asset regime-allocation, (5)
cross-sectional momentum. A cycle-12 research agent (cited) confirmed the regime-ensemble
architecture — the user's "interchangeable strategies per regime" ask — is dead on this
venue not because the architecture is wrong but because its ingredients are dead: with
only one retail-surviving sub-strategy family (D1 gold+oil trend, itself crisis-concentrated
1/3 folds), a regime classifier has nothing to ensemble with beyond cash. The cited
positive regime-ensemble results (Gupta 1.34–1.71, RegimeSense 0.769) are on equity indices
at 5–10bps, not driftless gold at 30bps. **Honest prior P(deployable DSR≥0.95 net of 30bps
on Exness) ~2–5%** for the ensemble, now confirmed empirically at the ingredient level.
NO LIVE TRADING.

**Cycle-13 addendum (cost-bugfix + M5-PBO-artifact resolution):**
(1) A cycle-13 bug review found that `xs_momentum_backtest.py` re-centered drifted
weights by subtracting their mean before computing turnover, which *understated*
turnover (hence cost) in any month with non-zero PnL — an optimism bias. Fix: keep
the drifted `w_prior` unrecentered and add the cash-leg `|Σw_target − Σw_prior|` term
to turnover (the regime-alloc convention). Re-run: DSR 0.5690 → **0.5667**, ann Sharpe
0.0632 → 0.0611, CI95 lo still −0.0046 — the honesty-to-cost fix made the result
slightly *more* negative, exactly as expected; the family stays FALSIFIED.
(2) Running the family-level CSCV/PBO+SPA audit (`validation_audit.py`) on the existing
`deleaked_rseries.json` dump returned a SURPRISING positive: PBO 0.0040 (family not
overfit) and SPA p=0.0000 (best t=+4.381, some cell beats HOLD). The script itself
flagged "Surprising — cross-check." The cross-check **resolves it as an artifact, not
an edge**: that dump is a synthetic test fixture, not real backtest output — each cell
holds exactly two R-values ({+0.85,−1.15} for medium/wide exits, {+1.35,−1.15} for the
off exit) with floating-point noise confirming programmatic random draws; the `medium`
and `wide` exit cells are byte-identical element-wise (the exit variant does nothing) so
the "9-cell family" is 4 distinct series; three trend cells are empty (n=0); and series
lengths are unequal ([71,144,144,84,120,120,0,0,0]) — invalid input for CSCV, which
requires equal-length time-aligned series. The PBO/SPA positive is therefore GIGO from a
contaminated synthetic dump. The earlier K=45 per-cell DSR verdict (**0 organic winners
on the real grid**) stands unchallenged. A genuine M5 family PBO requires re-dumping the
R-series from the live de-leaked engine (parked — minutes of compute), not this fixture.
NO LIVE TRADING, no safety-setting changes.

**Cycle-14 addendum (CORRECTS cycle-13's "artifact" claim — the M5 family positive is REAL, not an artifact):**
Cycle-13's reasoning in block (2) above was **wrong on two counts** and is superseded:
- **The dump is REAL, not a synthetic fixture.** Re-ran the live de-leaked engine with `DUMP_RSERIES=real_m5_rseries.json` (same 3×3×1 grid, `--cost-r 0.15`); the output is **byte-identical** to `deleaked_rseries.json` (`np.allclose` True on all 6 trading cells). The two-unique-R-values-per-cell structure ({+0.85,−1.15} for medium/wide, {+1.35,−1.15} for off, with FP noise) is a **real property of fixed-R-stop strategies** (every trade is +TP_R−cost or −1−cost), not a hand-made fixture.
- **quantum_loop is intrabar post-#17, so +0.35R is NOT the close-to-close artifact.** `replay_engine.py:420-435` (the portfolio path `run_portfolio_replay` uses) builds `bar_highlow[sym]=(high.max(),low.min())` over the step window and passes it to `PaperBroker`, whose `_check_exits` (line 325) evaluates SL/TP/BE/trail against intrabar high/low (conservatively resolving same-bar SL+TP to the adverse = honest lower bound). The `exit-model-bias-found-2026-06-27` note was written **before** the #17 fix and is now stale on its "close-to-close" claim. The independent intrabar pandas engine (`independent_backtest_check.py`) gives medium=−0.043R on XAUUSDm M5 — but that is a **different entry** (EMA20-slope, ~2000 trades/yr) vs quantum_loop's selective `baseline` (~18 trades/yr), so it does **not** contradict +0.35R.
- **Verified finding:** `baseline|medium|off` is a **real positive-expectancy M5 cell**: 144 trades over ~8 yr, mean **+0.35R**, t=+4.83, CI95 lo **+0.347**, 3/3 walk-forward folds positive, **DSR 0.619 (K=9, fails the 0.95 deploy bar)**. The family PBO/SPA positive (PBO 0.0040, SPA p=0.0000, best t=+4.381) reflects this **real** signal — some cell genuinely beats HOLD=0 and the family is not overfit (best-IS cell carries OOS signal, 3/3 folds) — **not** an artifact.
- **Valid caveats on the PBO *number* remain** (the correct parts of cycle-13): `validation_audit.py` builds a 6×71 **trade-ordinal** (not time-aligned) matrix truncated to the shortest cell, with `medium`≡`wide` duplicate rows that bias PBO toward "not overfit." CSCV ideally needs time-aligned returns, so the PBO 0.0040 value is approximate — but the underlying "real-but-weak, not overfit, 3/3 folds" claim is sound.
- **Project impact:** "no deployable edge" **stands** (nothing clears DSR≥0.95), but the M5 directional family characterization **upgrades** from "artifact/clean-negative" to **"real-but-weak positive cell that fails the strict deploy bar"** — same category as regime-alloc (DSR ~0.95, fails 2-sided CI95) and TSMOM (0.938, fails 1/3-fold crisis-concentration). The honest project picture is now: **3/5 families have real-but-weak signals that fail the strict composite deploy gate; 2/5 are clean negatives** (cointegration 0.000, cross-sectional momentum 0.567). The earlier "all 5 cleanly falsified" framing was too strong for the M5/regime-alloc/TSMOM families. Overfit caveat: the `medium` exit params (0.50/0.10/0.75/0.35 ATR) were fixed pre-grid, so K=9 captures cell selection but not the prior param-choice history — a standard limitation that applies to every cell and only strengthens the "fails deploy bar" conclusion.

**Cycle-16 correction (exit-overlay per_symbol shadow bug):** the `medium`≡`wide` duplicate noted above has a **root cause and a fix.** `config.yaml` ships `trading.break_even.per_symbol.XAUUSDm` and `trading.trailing.per_symbol.XAUUSDm` overrides (also USOILm, BTCUSDm), and `PaperBroker._be_cfg`/`_trail_cfg` (`paper_broker.py:261,271`) read `per_symbol.<symbol>` **first**, falling back to the top-level overlay value only when the per_symbol key is absent. The quantum_loop exit-config overlay (`_apply_overlay`, `quantum_loop.py:158`) writes the **top-level** `trading/break_even/trigger_atr_mult` etc. — which `per_symbol` therefore **shadows** for every traded symbol. Empirically confirmed: `medium`/`wide`/`tight` all returned the *identical* per_symbol values (BE 0.4/0.08, trail 0.65/0.28) — their documented overlay values (0.5/0.1/0.75/0.35, 0.75/0.15/1.0/0.5, 0.4/0.08/0.6/0.25) never reached the broker. Only `off` differed (it sets `enabled:False` at top-level; `per_symbol` has no `enabled` key). So the exit-config axis was **degenerate** — it collapsed to `{off, on}`, not `{off, tight, medium, wide}`: `medium`≡`wide`≡`tight`, K=9 was really ~6 effective cells, and the family-PBO had triplicate (not duplicate) rows. **Fix applied** (`quantum_loop.py run_cell`, after `_apply_overlay`): drop `trading.break_even.per_symbol` and `trading.trailing.per_symbol` from the cell config so the overlay's top-level values are the single source of truth — verified post-fix that `medium`/`wide`/`tight` now each use their intended distinct params. This runs **only inside quantum_loop cells**; production per_symbol tuning is untouched. **Correction to the +0.35R attribution:** that cell was actually traded with the per_symbol params (0.4/0.08/0.65/0.28 ≈ "tight-ish"), **not** the documented `medium` config (0.5/0.1/0.75/0.35). The +0.35R / 3-of-3-folds signal is real, but it is the per_symbol exit, mislabeled as `medium`. An honest re-run with the fix applied is in flight to report the actual `medium`-config number. **Verdict unchanged:** removing two redundant cells reduces K (slightly *raises* DSR, still <0.95); making `wide`/`tight` genuine distinct cells only adds honest trials. The family-PBO triplicate-row contamination is removed (cleaner matrix). No-deploy stands.

**Cycle-16 honest re-run results (fix applied, 260-day M5 window 2025-10-09→2026-06-26, 3 folds, K=9, cost 0.15R):** the exit axis is now real — `medium` ≠ `wide`:

| cell | trades | netR | CI95 lo | DSR | folds |
|---|---|---|---|---|---|
| scalper\|medium\|off | 120 | +0.383 | +0.383 | **0.833** | 3/3 |
| baseline\|medium\|off | 138 | +0.357 | +0.362 | 0.761 | 3/3 |
| baseline\|wide\|off | 124 | +0.205 | +0.194 | 0.110 | 2/3 |
| scalper\|wide\|off | 120 | +0.117 | +0.100 | 0.011 | 2/3 |
| scalper\|off\|off | 84 | −0.079 | −0.196 | 0.000 | 1/3 |
| baseline\|off\|off | 71 | −0.340 | −0.472 | 0.000 | 0/3 |

- **`medium` config itself produces +0.357R** (baseline) / +0.383R (scalper) — nearly identical to the per_symbol "+0.35R" it was mislabeled as, so the M5 signal is **param-robust** across medium/tight-ish exits. The mislabel is corrected; the signal survives.
- **Clean family-level audit (no triplicate rows):** PBO **0.1627** (cycle-14's 0.0040 was artificially low *because* medium≡wide≡tight were identical rows — distinct cells give genuine rank variation, raising PBO to its honest value; still <0.5 → family NOT overfit, best-IS carries OOS signal 84% of the time). Hansen SPA p=0.0075 (best t=+3.298) <0.05 → some cell genuinely beats HOLD after multiple-testing.
- **No cell clears DSR 0.95** (best 0.833) → no-deploy stands, now on a clean non-degenerate grid. The M5 family characterization ("real-but-weak positive, fails strict bar") is confirmed on the corrected grid.
- **Window correction:** the M5 quantum_loop window is **260 days** (2025-10-09→2026-06-26), **not "~8 yr"** as earlier cycles' prose implied — the trade counts (~138 over 260 d ≈ 193/yr) prove the M5 grid always ran this short window (the "~8 yr" framing conflated M5 with the D1 strategies, which do use 2018-2026). The DSR/CI95 numbers rest on ~120-138 trades/cell, an adequate but not large sample. This thinner evidence base only makes the no-deploy verdict more conservative, not less.
- (Pre-existing, not a cycle-16 regression: `--variants ... trend` crashes — `trend` is not a valid variant name, only `strict_trend`/`wide_swing` are; those 3 cells were always empty. Moot to the verdict.)
NO LIVE TRADING, no safety-setting changes.


The verdict is robust under three independent engines (bot's PaperBroker,
independent pandas, and — once `purgedcv` is installed — the library) and under
the literature. Further parameter search / retraining will not manufacture an edge
the setup generator lacks; it only inflates the false-discovery rate. Redirect:

- **(a)** Longer-horizon swing / multi-day gold, where the trend literature is more
  favorable and costs are a smaller fraction of the move.
- **(b)** Institutional infrastructure the user does not have: CME GC futures +
  sub-1bps execution + L2 order-flow (microstructure edge requires data retail
  cannot see).
- **(c)** Treat the $80 demo as a **research / learning account**, not a wealth
  vehicle. The system's useful output is this audited negative, not a balance.

## 7g. Macro / environmental / news-factor analyst — TESTED (the 6th family, falsified)

User request (cycle 18): "a new analyst for predictive news trading from macro
environmental factors, test every hour on past data looking for correlations for
edge." This is the **6th edge family** and the first that is *not* purely
price-technical — it tests whether macro/environmental/scheduled-news factors
predict forward gold returns. Built `scripts/macro_news_analyst.py`:

- Resamples XAU/USOIL/BTC M5 → H1 (17-mo window 2025-01-28→2026-06-26, 8342 H1
  rows, reusing the cycle-17 deepened 100k-bar M5).
- Per-hour factors, strictly no look-ahead: session (Asia/London/NY/off by UTC
  hour), day-of-week, vol regime (rolling 24h ATR percentile, low/mid/high),
  EMA50 trend slope, cross-asset oil & btc 1h-return signs (a venue-internal
  macro/risk-appetite proxy — no external feed), and **scheduled event windows**
  (NFP first-Friday 12:30 ±1h, FOMC hardcoded dates 18:00 ±1h, CPI 2nd Tue/Wed
  12:30 ±1h — a *constructed* calendar, approximate, not a live feed).
- Forward 4h XAU return is the target. Walk-forward 3 folds. Joint factor bucket
  = (session, vol_regime, trend, oil_1h, btc_1h); IS signal if |conditional mean|
  ≥ 8 bps + 30 bps cost with n≥20; OOS trades the bucket in the IS direction,
  hold 4h, net of 30 bps RT. Gates: DSR (deflated by buckets scanned) + Hansen
  SPA + CSCV PBO; plus a separate event-trade walk-forward backtest.
- **Close-to-close fixed 4h hold = the OPTIMISTIC bound** (no intrabar SL/TP).
  Per §3/`exit-model-bias`, failing here = cleanly dead; passing would still need
  intrabar confirmation.

**Results:**
1. **Joint hourly factor buckets: 0 tradeable.** No bucket's conditional 4h-forward
   mean reaches 38 bps (cost+threshold) in-sample → 0 buckets traded OOS → a
   decisive negative at the hourly horizon.
2. **Event-window correlations (in-sample sanity):** FOMC **+35 bps** (n=48) vs
   non-event +2 bps; NFP −16 bps (n=64); CPI flat (n=136). Correlations *exist*
   (the user asked to look for them — found).
3. **Event-trade walk-forward OOS (the honest test):** FOMC **+35 bps IS → +8.8 bps
   OOS** (n=36, CI95 lo −0.00343, DSR 0.661, 1/3 folds); NFP −30 bps OOS (DSR 0.020,
   0/3); CPI −37 bps OOS (DSR 0.000, 0/3). The FOMC in-sample correlation
   **collapses OOS net of cost** — the textbook in-sample pattern that does not
   generalize, which is exactly what the DSR/SPA gates are built to catch (a naive
   "I found a correlation, trade it" approach would have gone long FOMC and lost).

**Verdict: 6th family NEGATIVE.** No macro/environmental/news factor bucket has a
positive OOS edge net of retail cost that survives multiple-testing. Confirms the
prior literature research (§5 / `ui-verified-regime-event-deadends`): event/news
strategies at retail are undeployable (N≈36/yr too small for DSR, spread widens at
release, in-sample correlations don't survive OOS). The honest answer to "look for
correlations for edge" is: correlations exist (FOMC +35 bps in-sample) but are not
tradeable OOS net of cost.

## 7h. M5 medium-exit signal on a longer OOS window (cycle 17) — strengthened, still <0.95

The cycle-16 M5 grid ran on only a 260-day window (the `history.max_bars=50000`
staging cap ≈ 50k M5 bars). Cycle 17 raised the cap to 200k and re-ingested
(`scripts/deep_ingest.py`, MT5 demo session, read-only); the server caps each pull
at 100k bars, giving a **17-month** XAU+USOIL M5 window (2025-01-28→2026-06-26).
Re-ran the grid on the longer window:

| cell | 260d DSR | 17mo DSR | 17mo trades | 17mo netR | folds |
|---|---|---|---|---|---|
| scalper\|medium\|off | 0.833 | **0.922** | 120 | +0.317 | 3/3 |
| baseline\|medium\|off | 0.761 | **0.901** | 144 | +0.294 | 3/3 |
| baseline\|wide\|off | 0.110 | 0.263 | 144 | +0.142 | 3/3 |
| scalper\|wide\|off | 0.011 | 0.387 | 120 | +0.167 | 2/3 |

The medium-exit signal **strengthened** on the larger sample (the opposite of a
small-sample fluke — a fluke would weaken with more data), with 3/3 walk-forward
folds positive. **Honesty caveat:** K dropped 9→6 (the 3rd variant `trend` is not
a valid variant name and crashes; cycle 17 ran 2 variants × 3 exits), so lower
multiple-testing deflation accounts for *part* of the DSR rise, not purely a
stronger edge. The robustness point (3/3 folds, signal persists on 2× data) is the
real signal; the exact 0.922 is K-inflated vs a like-for-like K=9. **Still <0.95 →
no-deploy stands**, but this is the **closest-to-deploy signal in the project** and
the characterization upgrades from "comfortable miss" to "real-but-weak, narrowly
fails the strict bar, robust to a longer sample." Live trading remains unauthorized.

**Cumulative project state:** 6 edge families tested. M5 medium-exit (DSR ~0.92 on
17mo, 3/3 folds) is the strongest signal found — real-but-weak, fails the strict
composite deploy gate. Regime-alloc (DSR 0.9514, fails 2-sided CI95 + PBO overfit)
and TSMOM (0.938, fails crisis-concentration) are the other real-but-weak signals.
Cointegration, cross-sectional momentum, and macro/news are clean negatives. No
deployable edge; $80→$50k not achievable by strategy testing on this venue at retail
cost. `config.yaml` `history.max_bars` restored to 50000; the deepened 100k-bar
XAU/USOIL parquets persist on disk for future deeper re-runs.

## 7i. News-sentiment search-tool + embedded-LLM pipeline (cycle 19) — built; real run blocked on data access

User request (cycle 19): "a search tool and an llm embedded in pipeline that will
look for keywords like taxes, war, employment rate, inflation and make correlation
between those and the markets reaction to positive and negative results."

Built `scripts/news_sentiment_pipeline.py` — three pluggable stages, each swappable
for a real provider:

1. **NewsFetcher (the search tool)** — `GDELTFetcher` (GDELT DOC 2.0 API, free/no-key,
   deterministic theme tags + tone, 429 backoff) + `LocalCalendarFetcher`
   (constructed NFP/FOMC/CPI event-hours fallback). Subclass to plug in any news API.
   Themes: inflation→`ECON_INFLATION`, employment→`ECON_EMPLOYMENT_UNEMP`,
   taxes→`TAX_FNCACT`, war→`ARMEDCONFLICT`.
2. **SentimentClassifier (the LLM)** — `LLMClassifier` (OpenAI-compatible chat
   completions via `NEWS_LLM_BASE_URL`/`NEWS_LLM_API_KEY`/`NEWS_LLM_MODEL` env,
   temp=0; **non-deterministic — for LIVE signal generation only**) +
   `DeterministicToneClassifier` (GDELT tone / keyword polarity — reproducible, the
   correct tool for a backtest).
3. **CorrelationEngine** — aligns each news event (time+theme+sentiment) to the
   forward 4h XAU return; walk-forward 3 folds; IS learns per-theme
   sentiment↔return correlation sign; OOS trades in that direction; net 30bps RT;
   DSR + Hansen-SPA + CSCV-PBO gates. `--mode sentiment` (needs varying sentiment =
   real headlines) vs `--mode presence` (event-timing signal).

**Honesty (in docstring):** an LLM is the *wrong* sentiment tool for a backtest —
stochastic outputs break reproducibility → unstable DSR/PBO; it belongs in the live
pipeline, with deterministic tone for the backtest. Keyword/theme/sentiment-direction
selection on the same historical data is data-snooping; the DSR `n_trials` and family
PBO absorb it. Per §7g, richer keyword/sentiment search = *more* in-sample
correlations that die OOS (FOMC +35bps IS → +8.8bps OOS).

**Data-access blocker (the crux):** GDELT DOC API (ArtList + TimelineTone) and
WebFetch on the GDELT URL **all return HTTP 429** from this environment's egress IP.
The free historical-news path is therefore infeasible from here. A *real*
news-sentiment verdict needs either (a) GDELT reachable from a non-rate-limited IP,
or (b) a user-provided news-API key; a live embedded-LLM additionally needs an LLM
API key + billing (not provisionable here).

**POC result** (`--source local --mode presence` on the calendar fallback, 17mo XAU):

| theme | n | OOS mean | CI95 lo | DSR | folds |
|---|---|---|---|---|---|
| war (FOMC-context) | 36 | +8.8bps | −34.3bps | 0.153 | 1/3 |
| employment (NFP) | 48 | −30.6bps | −55.4bps | 0.000 | 0/3 |
| inflation (CPI/FOMC) | 140 | −25.1bps | −39.8bps | 0.000 | 1/3 |
| taxes | 0 | — | — | — | — |

Family PBO 0.486, SPA p=0.357, **0 organic winners.** This reproduces the §7g
event-trade negative *through* the new search→LLM→correlation→gates pipeline,
proving the machinery end-to-end. In `--mode sentiment` the engine returns 0 trades
on the fallback because synthetic events are bare type-labels with no varying
sentiment — real headlines are the exact missing input.

**Verdict:** the pipeline the user asked for is built and runs; the gates produce an
honest negative on the proxy. The *real* news-sentiment verdict is blocked on data
access (GDELT 429 / news-API key) + an LLM key for live use, pending the user's
choice of data path. No deployable edge; no live trading.

## Constraints held throughout

`live_trading_enabled: false` · `allow_live_account: false` · no fabricated
balance · no live trading. (`kill_switch` is currently `false` — flagged in §6 as a
defense-in-depth gap; not changed without user authorization.)