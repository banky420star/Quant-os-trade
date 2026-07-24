# Best settings — how to run it responsibly

There is no settings combination that *makes* a trading bot profitable — profit
comes from a real edge in the strategy, which only your own results can prove.
What follows is the **best way to run it**: a conservative, disciplined,
paper-first configuration that risks nothing while you find out whether the edge
is real, then a clear path to graduate to live money.

These are the defaults now shipped in `config.yaml`. You don't have to change
anything to get them — this doc explains *why* they're set the way they are.

---

## 1. Start on paper (profile `30`)

```powershell
python scripts\kill_agent.bat
python scripts\preflight.py
python start.py --profile 30
```

Dashboard: **http://127.0.0.1:8080**

Profile `30` runs the **entire pipeline** — signals, verification, conviction
grading, execution, position management, the self-learning loop, the dashboard —
but places **no real orders**. This is where you live for a while. Watch:

- **Daily P&L panel** — is it green over days, not just one lucky session?
- **Self-Learning panel** — does the monitor trend `improving` and stay
  positive-expectancy, or flip to `degrading`?
- **Recent Signals → Grade column** — are the A/A+ setups the ones that win?

Only move to live money when paper shows a positive, stable edge across enough
trades that it isn't luck. Your own `docs/research/VERDICT.md` is a *negative*
backtest — treat that as the bar to clear.

---

## 2. The professional risk rails (already set)

| Setting | Value | Why |
|---|---|---|
| `conviction.min_grade_to_trade` | **B** | Skip low-conviction C setups entirely — trade fewer, better. |
| `conviction.size_by_grade` | A/A+ 1.0×, B 0.6× | Full size on high conviction, half-size probes on B. |
| `signals.min_risk_reward` | **1.05** | Never take a trade whose reward doesn't beat its risk. |
| `trading.allow_pyramiding` | **false** | No adding to positions — it multiplies drawdown faster than edge. |
| `trading.max_open_per_symbol` | **1** | One idea per symbol at a time. |
| `learning.overtrading_cap` | **4** | Hard cap on entries per window — kills revenge-trading. |
| `risk.max_daily_loss_pct` | **5%** | Pause the day before a bad run cascades. The #1 ruin control. |
| `risk.max_consecutive_losses` | **4** | Stop after 4 losses in a row and reassess. |
| `risk.daily_profit_halt_usd` | **15** | Bank the day once you're up ~12% on a micro account. |

To be **even more selective**, raise `conviction.min_grade_to_trade` to `A`
(only the very best setups trade). To trade **more often**, drop it to `C`
(everything trades, but weak setups are auto-sized down to 0.3×).

---

## 3. Graduating to micro-live (profile `30-real`)

Only after paper proves out:

```powershell
python start.py --profile 30-real
```

This is **real money** — tiny, but real (0.01 lot, $10 max loss per trade).

**Important — the micro exposure cap:** on `30-real` the exposure limit is
~$12 per symbol. A single 0.01 lot of **gold or an index is ~$26+ of notional**,
so those instruments will be correctly rejected (`exposure_limit_exceeded`) and
never trade. On the micro account, either:

- trade **low-notional symbols** (most FX pairs fit a 0.01 lot under the cap), or
- deliberately raise `practice.micro.max_symbol_exposure_fraction` if you want
  gold/indices — understanding that means more risk per position.

---

## 4. What to watch, and when to stop it

- **Self-Learning monitor says `degrading` with rollback advised** → believe it.
  Recent trades are doing worse than baseline; stop and review.
- **Daily-loss halt or consecutive-loss halt fires repeatedly** → the setup
  isn't working in current conditions. Don't override it — that's the rail
  doing its job.
- **A promoted shadow experiment** → it beat the live baseline out-of-sample.
  Review it before applying; promotion is evidence, not a command.

---

## The one honest sentence

Run it on paper, keep the rails on, let it be selective, and let the monitor
tell you the truth — these settings maximize your odds of *surviving long enough
to find out if there's an edge*, which is the only thing that lets one compound.
