# Quant OS Trade — Consolidated Review + Improvements (2026-07-31)

## Scope

A review of the MT5 retail quant trading bot ("Quant OS Trade") at
`C:\Users\Administrator\Desktop\new task`: results + logic behind the
improvements, the model and system architecture, wiring/ingestion/process
integrity, and where an ML/LLM/AI upgrade integrated into the pipeline could
genuinely help. This document is paired with the no-deployable-edge verdict in
`VERDICT.md` (19 cycles, 6 edge families) — read both: VERDICT is *what the
research proved about edge*, REVIEW is *what the live system does, what broke,
and what was fixed/upgraded this session*.

## TL;DR

1. **A real account was wiped to $0** on 2026-07-30. Root cause is fixed
   (micro-account ruin guard, §3). The bot is on **demo**; **no live trading**
   is authorized or active.
2. The honest research conclusion stands: **no deployable edge at retail 30bps**
   across all 6 edge families (M5 directional, D1 Donchian, TSMOM,
   regime-alloc, cointegration, cross-sectional momentum). "Winning" here means
   *not destroying capital* and fixing the live/research consistency gaps that
   caused the wipe — not manufacturing edge.
3. Five concrete fixes shipped this session (§5): a per-trade equity-fraction
   risk cap, a broker-comment setup-name truncation repair, a policy_optimizer
   extractor fix, a cost-aware culturing veto, and a shadow Thompson-sampling
   bandit + a wired-in LLM news-sentiment shadow risk filter (§6). A sixth fix —
   a **setup-level aggregate veto** (§7) — directly kills the #1 loss driver the
   MT5-journal review found (pullback: 66% of all trades, 33% win rate).

## 1. System architecture (what it is)

- **17-loop sequential pipeline** (`core/pipeline.py:run_pipeline`), ~45s/cycle.
  Fast loops (data → feature → market_context → risk → signal → evaluation →
  verifier → execution …) run every cycle; **analytical loops** (policy_detection,
  policy_optimizer, adaptation, news_sentiment) run every 5 cycles.
- **25 verifier gates** (`core/verifier.py`) — kill_switch, valid_levels,
  exposure, symbol_capacity, data_driven_veto, positive_evolution, news, etc.
  Each gate is fault-isolated; a failure in one loop never stops the rest.
- **State-file IPC** — the whole bot communicates via JSON in `state/`
  (account, kill_switch, runtime_mode, trade_log, forward_test_ledger,
  symbol_policy_live, culturing/<symbol>, policy_scores, best_policies,
  regime_evolution, news_sentiment_snapshot, culturing_bandit, …).
- **Adaptive weights** (`core/adaptation_engine.py`, `kelly_sizing.py`) are
  closed-form credit-assignment / Kelly, **not ML**. The PPO/RL stack is NOT in
  this repo (no torch / stable-basines3) — the "model" is the rule-based signal
  generator + the verifier gate set.

## 2. The wipe (root cause)

On 2026-07-30 a single NAS100m SELL pullback (trade 527967490) lost **-$79.64**
on ~$100 equity (`risk_amount` $103.82). The real account 295959027 went to $0;
the kill switch tripped at 100% drawdown. Three things lined up:

1. **Min-lot floor overrides Kelly sizing upward.** 0.01 lot NAS100m risks
   ~$104 at the SL — one stop-out = ~100% of a $100 account.
2. **Exposure caps are multiples of equity** (growth profile 6x/12x), so a
   *single* min-lot position whose SL risk exceeds the whole account is never
   caught by an exposure cap (it's a *per-position* risk problem, not a
   notional/aggregate one).
3. **The growth profile set no `max_loss_per_trade_usd`** and
   `cap_loss_to_balance=false`, so `effective_risk_cap()` returned `None` and
   the per-trade USD cap gate was **skipped entirely** — the exact hole the
   min-lot floor sailed through.

A separate, dangerous `account_mode` mismatch also existed: `account.json`
said `real` while `runtime_mode.json` said `demo`.

## 3. Fix #1 — equity-fraction per-trade risk cap (wipe prevention)

`core/position_sizing.py:calc_executable_volume` now has a guard that fires
**exactly when no explicit per-trade cap is configured** (`cap_usd is None`,
the growth-profile hole). When tick specs are available, it rejects any trade
whose realized SL risk at the final (possibly min-lot-floored) volume exceeds
`risk.max_risk_per_trade_pct` (default 5%) of equity.

- On a $100 account, a trade may risk at most $5 when no cap is set → the
  $104-risk NAS100m min-lot is **correctly rejected** as un-tradable.
- Profiles that explicitly set `max_loss_per_trade_usd` (e.g. micro) keep their
  own cap and are **not** double-gated (`and cap_usd is None`).
- `config.yaml`: `risk.enforce_equity_risk_cap: true`, `max_risk_per_trade_pct: 5`.

38 tests pass including the wipe-rejection and a $5000 funded-account pass.

## 4. Fix #2 — broker-comment setup-name truncation

MT5 truncates order comments to 16 chars at the broker. A comment written as
`qagent_donchian_breakout` returns as `qagent_donchian_` → extracted setup
`donchian_` (9 chars). This **split one setup across two culturing cells**
(`donchian_` n=15 vs `donchian_breakout` n=151; `ma_crosso` n=14 vs
`ma_crossover` n=13) so neither cell reached a robust sample and the
data-driven veto could never act on the full evidence.

- `core/strategy_policy.py:normalize_setup_type` now prefix-matches a truncated
  label to its single canonical full name (`ma_crosso`→`ma_crossover`,
  `donchian_`→`donchian_breakout`, `mean_reve`→`mean_reversion`,
  `false_bre`→`false_breakout`). Ambiguous/short prefixes are left unchanged,
  never guessed.
- `core/position_sync.py:_setup_type_from_comment` now routes through
  `normalize_setup_type` (deferred import, no cycle) so the repair applies at
  the comment-extraction source.
- Verified: split cells now collapse to one culturing cell key. 72
  exposure/setup/diversification tests pass.

## 5. Fix #3 — policy_optimizer extractor

`policy_scores.json` carries a **flat top-level `variants` list** (576 entries)
plus a `best_by_key` dict, but `_extract_policy_candidates` only looked for
`policies`/`cells` keys — neither exists → it returned `{}` and
`best_policies.json` was **empty despite 576 variants**. Fixed to handle all
three shapes (cell-keyed dict, flat variants list grouped by
`symbol|setup|session`, and `best_by_key` fallback). Now resolves 45 cells /
576 variants. 12 policy tests pass.

## 6. Fix #4 — cost-aware culturing veto

The culturing veto used `cost_r=0.0` while the research verdict uses 30bps.
Investigation: all 1411 ledger records are real/demo MT5 **fills** (they carry
`mt5_deal`/`mt5_position`), and R is computed from actual fill prices
(`entry`/`exit`), so **the spread is already in the realized R** — `cost_r=0`
is correct, not a double-charge. The honest fix:

- `loops/forward_test_loop.py` now tags each cell with a `cost_basis`
  (`fill`/`paper`/`mixed`). Fill-only cells keep `cost_r=0` (spread in the
  fill). Only **paper-simulated** records (no `mt5_deal`, mid-price entries
  whose R omits the spread) pull in a conservative `paper_cost_r` burden.
- The gap is now **explicit and auditable** per cell in
  `state/forward_test_ledger.json`. 22 evolution tests pass; current all-fill
  data is unchanged behavior.

## Fix #6 — setup-level aggregate veto (the direct MT5-journal fix)

The MT5-journal review (this session) found **pullback was 66% of all 1411
trades at a 36% win rate and −0.306R expectancy** — the single biggest loss
driver — yet no culturing veto ever fired on it. The reason is structural: the
data-driven veto is **per-cell** (`symbol|setup|regime|align|session`), so
pullback's losing evidence is fragmented across ~50 cells and no single cell
reaches the veto threshold. The journal review recommended a coarser
setup-level gate; that gate is now built and verified on the real data.

- `loops/forward_test_loop.py:_build_setup_aggregates` buckets closed trades by
  **normalized setup** (broker-truncation repaired via Fix #2), runs the same
  `compute_stats` per setup, and vetoes when `n ≥ min_n (100) AND net
  expectancy < 0 AND win rate < 40%`. `min_n=100` is deliberately high so a
  setup-level call is only made on real evidence, never on noise. Setups below
  `min_n` stay `thin` — keep collecting.
- The vetoed setup list is written **top-level** to
  `state/symbol_policy_live.json` (`vetoed_setups`) alongside the per-symbol
  cell veto. `build_ledger` also returns `setup_aggregates` (per-setup stats +
  verdict) to `state/forward_test_ledger.json` for audit.
- `core/verifier.py` adds a `setup_aggregate_veto` check that hard-rejects any
  signal whose normalized `setup_type` is in `vetoed_setups` (empty/missing →
  permissive). It is a **hard gate** (failure-code mapped, not a soft check), so
  a vetoed setup cannot pass even when everything else is clean.
- `config.yaml: culturing.setup_aggregate_veto: {enabled: true, min_n: 100,
  veto_win_rate_pct: 40, veto_net_r: 0.0}`.

**Verified on the real 1411-trade ledger:** `vetoed_setups = ['donchian_breakout',
'pullback']` — pullback (928 trades, 36.4% wr, −0.306R) and donchian_breakout
(166 trades, 31.3% wr, −0.185R) are both blocked. `trend_continuation` (150
trades, 40.7% wr) correctly stays `ok` — it sits at the win-rate floor and the
gate requires BOTH netR<0 AND wr<floor. 10 new tests pass; 779-test regression
sweep is clean (the single `test_walk_forward_microtest` failure is a
pre-existing subprocess+monkeypatch bug that reads the real 1411-trade state,
unrelated to this change). This is pure risk-reduction on demo — no live
trading, no orders.

## Evolution loop — specialized ICT/SMC killzone setups (hourly)

User direction: search TradingView for more specialized setups and enforce them
into the system to keep it evolving, distinguishing per-symbol what works.
Scheduled as a **session-only hourly cron** (`c8f07194`, fires :07 each hour;
the durable `/schedule` cloud skill isn't installed in this env, so it's
session-only and 7-day auto-expiry). Each iteration adds one specialized setup
as a `SetupSpec` appended to `SPECIALIZED_SETUPS`.

**Iteration 1 (2026-07-31):** two ICT/SMC killzone setups sourced from TradingView
community indicators (Gold SMC Dashboard, Smart Money Gold Map, XAU/USD
Killzones 2026 playbook):

- `silver_bullet` — 14:00–15:00 UTC session-liquidity sweep + rejection (CHoCH
  proxy) → BUY on bullish_rejection / SELL on bearish_rejection.
- `london_judas` — 07:00–10:00 UTC London Open Judas Swing (manipulation
  reversal) — same rejection+liquidity proxy, different window → different
  culturing cell so the ledger distinguishes the two per symbol independently.

Wired as first-class setups (`setup_library`, `setup_triggers`,
`setup_classifier`) and detected by `core/specialized_setups.py:detect_specialized`,
which `SetupClassifier.classify`/`classify_all` call when enabled. **Opt-in** via
`signals.specialized_setups: {enabled: false, symbols: [], setups: []}` — OFF by
default, with per-symbol and per-setup allowlists so the loop can trial one
setup on one symbol at a time as evidence accumulates. 15 new tests pass;
798-test broad sweep green (the single `test_walk_forward_microtest` failure is
the pre-existing subprocess+monkeypatch bug).

**Honest framing:** per VERDICT.md there is no deployable edge at retail 30bps;
ICT/SMC killzone concepts are a popular social framework, not a proven edge.
Adding them does NOT create edge. The value is **granularity**: new setup_types
→ new culturing cells → the data-driven veto + Thompson bandit + setup-aggregate
veto distinguish per-symbol which setups work and which lose. When enabled they
flow through every existing verifier gate; if one loses on a symbol, culturing
blocks it. To start collecting evidence, flip `signals.specialized_setups.enabled:
true` (optionally `symbols: ["XAUUSDm"]` to trial on gold first). Bot stays on
demo; no live trading, no orders.

## 7. Upgrade #1 — Thompson-sampling bandit over the culturing ledger (shadow)

The data-driven veto is a **hard binary gate** (`n≥8 AND netR<0 AND wr<floor`).
A cell at n=8, netR=-0.05, wr=39% is vetoed forever, the same as a cell at n=8,
netR=-0.40, wr=20%. `core/culturing_bandit.py` replaces that with a continuous
**conjugate-Normal posterior** over each cell's true mean R:

- **Skeptical prior** (`prior_mean_r=0`, `prior_weight=8`=min_n): a cell needs
  ~min_n trades of its own data to escape the "no edge" base rate. A single
  n=1 cell at raw `-1.063R` regresses to posterior `-0.118R` — the bandit
  refuses to over-react to one bad trade.
- **Thompson sampling**: each round draws a selection score from the posterior;
  thin cells keep wider posteriors (`thin_exploration_scale`) so they keep
  being explored, well-sampled cells tighten (se 0.049→0.012).
- **Shadow only**: writes `state/culturing_bandit.json`; **never touches
  `symbol_policy_live.json`** (the hard veto the verifier reads). Default off
  (`culturing.bandit.enabled: false`). Wired into `forward_test_loop.run`.
- 9 tests pass. This is the principled ML replacement for the binary veto —
  sample-size-aware, exploration-preserving, honest under "no edge".

## 8. Upgrade #2 — LLM news-sentiment shadow risk filter (wiring the cycle-19 pipeline)

The cycle-19 news pipeline (`scripts/news_sentiment_pipeline.py`:
RSSNewsFetcher + OllamaClassifier + LLMClassifier) was built and verified but
**never wired into the live bot**. Now wired:

- `core/news_sentiment.py` — fetches recent RSS headlines, classifies polarity
  via Ollama/LLM (the right tool for *live* — non-reproducible is fine; falls
  back to deterministic keyword polarity on any failure), aggregates per-theme +
  detects a **strong hint** (≥`strong_hint_min_items` headlines at ≥`strong_hint_threshold`
  magnitude). Writes `state/news_sentiment_snapshot.json`.
- `loops/news_sentiment_loop.py` — registered as an **analytical loop**,
  self-throttled by `news.refresh_interval_seconds` (default 900s), no-op when
  disabled, network-failure-tolerant.
- `core/verifier.py` — a new `news_sentiment` check is **shadow by default**
  (`news.sentiment_gate: false` → always passes, records the hint). An operator
  can turn the gate on so a strong bearish macro-news backdrop hard-rejects new
  BUY entries (risk-on caution) and vice versa. Never blocks on missing data.
- 7 tests pass. Per the research verdict (no tradeable news edge at 30bps) this
  is **risk hygiene, not a signal** — and it's the LLM integration the user
  asked for, applied where it belongs (live, as a filter).

## 9. Where an ML/LLM/AI upgrade can actually help (honest tiers)

Ranked by *expected value under the no-edge verdict* — i.e. by how much they
reduce capital destruction / improve decision quality, not by how much edge
they manufacture.

| Tier | Upgrade | Why it helps | Status |
|---|---|---|---|
| 1 | **Equity-fraction risk cap** | Stops the exact wipe mode. Not ML, but the highest-EV change. | **Shipped** |
| 2 | **Thompson bandit over culturing cells** | Replaces a brittle binary veto with a sample-size-aware posterior. Honest under no-edge. | **Shipped (shadow)** |
| 3 | **LLM news-sentiment as risk filter** | LLM in its correct role: live polarity for *caution*, not trade signals. | **Shipped (shadow)** |
| 4 | **Setup-name repair + extractor fix** | Makes the data-driven layer actually see full evidence (was split/empty). | **Shipped** |
| 5 | **Regime-conditional cell model** | A small hierarchical Bayesian model over `setup|regime|align|session` cells (the bandit generalised to a joint posterior). More honest shrinkage across correlated cells. | Next |
| 6 | **LLM trade-narrative → outcome classifier** | The bot already writes `be_narrative`/`exit_narrative` per trade. An LLM could cluster narratives into outcome-conditioned families (a qualitative feature the bandit can bucket on). | Next |
| 7 | **PPO/RL entry policy** | Only if/when a positive-R cell family survives the DSR/SPA/PBO gates. Currently no edge → RL would optimize a negative-expectancy policy (the 06-30 dead-Gaussian local minimum). **Do not deploy.** | Blocked on edge |

The honest rule: **ML cannot manufacture edge where none exists.** It can
allocate exposure toward the least-bad cells (bandit), filter risk (news), and
make the data-driven layer see its own evidence correctly (repairs). That is
the entire envelope of value here until a positive-R family clears the gates.

## 10. What was NOT done (deliberately, user's decisions)

- **Kill switch was not cleared.** It stays tripped.
- **Live trading was not restarted.** Bot is on demo.
- **No profile switch, no orders placed.**
- `account_mode` mismatch (account.json `real` vs runtime_mode.json `demo`) is
  **surfaced here but not auto-corrected** — reconciling it is the user's call,
  since it determines real-vs-demo and the user controls that surface.

## 11. Recommendations

1. Reconcile `account_mode` across `account.json` / `runtime_mode.json` /
   `config.yaml:mode` before any restart — the mismatch is how demo work leaks
   into a real account.
2. Keep the bot on demo; let the culturing ledger + bandit accumulate evidence
   under the new (un-split) cells. The first honest signal will be whether any
   cell's posterior mean climbs and **stays** above 0 with n>min_n — that is
   the only data point worth acting on.
3. If a positive-R cell family ever clears the composite gate (DSR≥0.95 AND
   two-sided CI95 lo>0 AND fold-robust), *then* revisit tier 7 (RL) — and only
   then.
4. The LLM news filter can be promoted from shadow to gate once you have
   enough live evidence that strong-hint backdrops correlate with your
   realised losses (collect the shadow data first).

---

*NO LIVE TRADING. All changes default-off where they touch trade decisions.
Research verdict (VERDICT.md) unchanged: no deployable edge at retail 30bps.*