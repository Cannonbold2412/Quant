# TRD — AQRL Technical Requirements

> **Status:** Living document. Updated after every design session.
> **Last updated:** 2026-07-27
> **Companion docs:** `PRD.md` (why), `Backend-Schema.md` (data), `App-Flow.md` (sequences), `Implementation_Plan.md` (build order)

---

## 1. Architectural Stance

AQRL is **not an "AI agent" project**. It is a distributed research system that happens to use an LLM for the reasoning steps. The mental model is a job queue with workers, not a chatbot with tools.

Three rules that follow from this:

1. **Claude only thinks.** Collectors, schedulers, databases, backtests, and statistics are ordinary Python. Claude must never spend tokens downloading a PDF or renaming a file.
2. **Claude is stateless.** One session per job, then destroyed. The database holds all state. This makes the system restartable, parallelizable, and debuggable.
3. **Agents know queues, not each other.** No agent calls another agent directly. They read from and write to queues. This is what makes 1 worker and 100 workers the same code.

### 1.1 The four components

| Component | Role | Implementation |
|---|---|---|
| **Brain** | Reasoning | Claude Code sessions, one per job |
| **Hands** | Execution | Backtest engine, statistical battery, data layer |
| **Memory** | Accumulated knowledge | Experiment DB + knowledge graph + vector index |
| **Nervous System** | Coordination | Scheduler, job queue, state machine, budget control |

The Nervous System is the piece most projects omit. Without it you have a collection of scripts; with it you have a laboratory.

---

## 2. Runtime Topology

### 2.1 v1 — laptop (target for first build)

Exactly **one always-running process**: `scheduler.py`. Everything else is invoked on demand.

```
scheduler.py  (tick loop, ~60s)
      │
      ├─ reads jobs table (SQLite)
      ├─ dispatches ready jobs to workers
      ├─ enforces budgets & concurrency caps
      └─ updates state machine

workers (subprocess, on demand)
      ├─ agent1_research.py    → Claude session
      ├─ agent2_engineer.py    → Claude session
      ├─ agent3_reviewer.py    → Claude session
      ├─ agent4_promotion.py   → Claude session
      ├─ agent5_knowledge.py   → Claude session
      ├─ evaluate.py           → pure Python, no LLM
      └─ collectors/*.py       → pure Python, no LLM
```

Single scheduler is dramatically easier to debug than five independent daemons. This is deliberate.

### 2.2 Scaling path (do not build yet, but do not preclude)

| Phase | Change |
|---|---|
| **v1** | SQLite, one scheduler, sequential execution, one Claude session per job |
| **v2** | Multiple A2/evaluate workers in parallel, Redis job queue, Docker isolation |
| **v3** | Multiple machines or cloud VMs, PostgreSQL, shared object storage, Kubernetes |

The v1→v3 migration must be a **swap of the queue and DB backends only**. Nothing in agent logic may assume SQLite or single-machine execution. Concretely: no file locks as coordination, no in-process shared state, all job claims via atomic DB transactions with a lease/heartbeat.

### 2.3 Eventual target hardware (reference only — not v1)

Documented so architecture does not preclude it:

- **Control plane** (24/7): 8–16 vCPU, 32–64 GB RAM, 1 TB SSD — scheduler, Postgres, vector DB, job queue, dashboard API. No GPU.
- **Research nodes** (CPU cluster): ~20 × (32 vCPU, 128 GB RAM, 2 TB NVMe) — backtests, walk-forward, Monte Carlo, parameter search. Embarrassingly parallel.
- **AI nodes** (GPU): 4 nodes × 2 L40S / 4 RTX 6000 Ada / H100 — embeddings, rerankers, OCR, local LLMs. Claude Code itself needs no GPU.
- **Storage**: start at ~100 TB object storage. Experiment output grows faster than intuition suggests.

---

## 2A. nanoAQRL — what v1 actually is ★

**Everything in §2.1 is the destination. This section is the starting point.**

The reference project (PRD §14) runs a complete autonomous research loop in three files. Our v1 mirrors that shape exactly. The five-agent architecture, the job queue, the knowledge graph and the full schema are **later stages**, added when a felt need arrives — not built up front.

### 2A.1 The files

| File | Contents | Agent permission |
|---|---|---|
| `data.py` | Snapshots, calendars, cost models, universe definition | **read only** |
| `strategy.py` | Signal logic, entries, exits, filters, sizing | **the only writable file** |
| `evaluate.py` | The scoring harness | **neither readable nor writable** |
| `program.md` | The agent's operating instructions and the acceptance bar | **human-edited only** |
| `results.tsv` | `commit \| score \| n_trades \| status \| description` | append only |

### 2A.2 The loop

```
edit strategy.py → commit → run evaluate.py → read score
      → clears the bar?  keep the commit
      → worse/equal?     git reset
      → append one row to results.tsv
      → repeat, unattended
```

Status values are exactly three: `keep` · `discard` · `crash`. Every experiment gets one. Forcing a verdict on every run prevents results piling up unjudged.

### 2A.3 One deliberate deviation from the reference

The reference lets the agent **read** `prepare.py`; only editing is forbidden. We forbid **reading** `evaluate.py` as well.

Justification: `val_bpb` survives being understood — knowing how a held-out likelihood is computed does not help you fake one. A backtest score does not survive being understood. An agent that can read the scorer will eventually exploit a weakness in it, not from malice but because exploiting the measurement is the cheapest path to a higher number. See §8A.1.

### 2A.3a What `program.md` must contain ★

Since the agent cannot read `evaluate.py` (§2A.3), `program.md` is the **only** channel through which it learns what will be checked. That forces a deliberate split:

| Reveal | Hide |
|---|---|
| **Correctness rules** — the look-ahead prohibitions below. Not gameable; hiding them only causes avoidable failures | **The scoring formula and its thresholds.** These *are* gameable — an agent that knows the exact haircut can aim at it |
| The bar's *dimensions* (trades, drawdown, breadth, complexity exist) | The bar's *numbers* |

**These instructions reduce the error rate. They do not enforce anything.** The agent cannot grade its own look-ahead; P0 (§5.1) remains the enforcement layer. Both, always.

#### Required content — anti-look-ahead rules

**Signal timing**
- Every signal is computed from data available at or before bar *t*, and acted on at *t+1* or later per the fill model.
- Explicitly lag every signal before combining it with returns. Never multiply a same-bar signal by a same-bar return.
- `shift(-n)` is forbidden anywhere, for any reason.

**Statistics and normalisation**
- Rolling statistics only. Never compute mean, standard deviation, z-score or percentile rank over the whole series.
- No centred windows (`center=True`).
- Any fitted transform — scaler, PCA, threshold — is fitted on **training data only** and applied to test data.
- Thresholds are derived from the training window, never from the full sample.

**Missing data**
- `bfill()` / `fillna(method='backfill')` is forbidden — it pulls the future backwards.
- Forward-fill only, and be explicit about why it is safe.

**Resampling and joins**
- A bar's own close is not known until that bar closes. Do not use it to decide an action inside the same bar.
- Timestamp merges must not silently align future data to past rows.

**Fitting**
- Parameter tuning happens **inside the fold, on the training window only** (§4A.2i). If tuning ever touches the test window, the fold is worthless.

**Verification duty**
- Before committing, state in the commit message which bars each signal reads and what its lag is. Articulating the timing catches most errors before they reach P0.

#### Required content — behavioural rules

- **A P0 rejection is a bug in your code, not an obstacle.** Fix the cause. Do not restructure code to get past the check while preserving the behaviour — that is the failure mode this whole architecture exists to prevent.
- **Prefer the simpler strategy** where results are close (tertiary criterion, §4A.3).
- **Stop when the bar is cleared.** Do not keep searching for a higher number (PRD §13.2).
- **Do not pause to ask the human whether to continue.**

### 2A.4 Storage decision — SQLite *and* git, not either/or

The reference uses git as the entire experiment database. We do not, for one decisive reason: **deflated Sharpe requires a trial count**, and "how many attempts have been made in this family?" cannot be answered by grepping `git log`. Paper trading, health monitoring and deployment state reinforce the same conclusion.

His loop is one file evolving in a straight line; ours produces many independent candidates that form no single lineage, so the keep/reset ratchet does not map cleanly.

The split:

| Store | Holds |
|---|---|
| **git** | The actual strategy code, diffs, history |
| **SQLite** | Metadata, metrics, provenance, state, trial counts |
| **`experiments.code_commit`** | The hash linking the two |

Strategy code is **never** stored as a blob in the database — that loses diffs, blame, and the ability to check out and re-run a past experiment.

### 2A.5 Minimum viable schema

`Backend-Schema.md` defines 15+ tables. Building all of them before running one experiment is designing the archive before doing the science. v1 starts with **three**:

- `strategies` — the research thread, carrying `family` for trial counting
- `experiments` — one row per attempt: provenance, `code_commit`, status
- `evaluations` — the metrics from `evaluate.py`

`jobs` is added with the scheduler. Everything else is added on felt need. The designs already exist, so later addition is cheap.

---

## 3. Execution Model

### 3.1 Event-driven, not timer-driven

Agents do not poll in a `while True` loop burning compute. State transitions emit events; the scheduler converts events into jobs.

```
Strategy spec saved      → enqueue IMPLEMENT job
Evaluation completed     → enqueue REVIEW job
Review says "iterate"    → enqueue IMPLEMENT job (n+1)
Review says "done"       → enqueue PROMOTE job
Promotion decided        → enqueue ARCHIVE job + notify dashboard
Paper trading milestone  → enqueue MONITOR job
Health check trips Red   → enqueue lifecycle action + notify dashboard
New paper ingested       → enqueue EXTRACT job
Failure pattern detected → enqueue research question to curiosity queue
```

The system is effectively 24/7 but consumes compute only when there is work.

### 3.2 Time-driven jobs (the exceptions)

Genuine schedules, owned by the scheduler:

| Cadence | Job |
|---|---|
| Hourly | Poll paper/GitHub/blog collectors for new items |
| Daily (post-close, per market) | Ingest market data; recompute regime/volatility/correlation stats |
| Nightly | A1 hypothesis batch generation; daily research report |
| Weekly | A5 cross-experiment pattern mining; knowledge-graph consolidation |
| Weekend | Large parameter sweeps, portfolio optimization, deep literature review |

### 3.3 Job lifecycle & failure handling

`PENDING → CLAIMED → RUNNING → {SUCCEEDED | FAILED | TIMED_OUT}`

- Claims are atomic with a **lease** and heartbeat; a dead worker's lease expires and the job returns to `PENDING`.
- Retries: bounded, with exponential backoff. Distinguish **transient** (API timeout, rate limit) from **deterministic** (code won't compile) failures — the latter must not retry blindly; they route to A2 as a fix-job with the error attached.
- Every job records: inputs hash, outputs, duration, token/compute cost, and error trace.
- **Poison-pill protection:** a strategy whose jobs fail *k* times consecutively is quarantined for human inspection rather than looping forever.

### 3.4 Budgets and back-pressure

Hard caps enforced by the scheduler, configurable:

- Max concurrent Claude sessions
- Max tokens per day (global) and per strategy (lifetime)
- Max iterations per strategy (backstop to the evidence-based stop rule)
- Max wall-clock per evaluation phase
- Max experiments per day

When a cap is hit, the scheduler stops dispatching new work of that class and logs the reason. Budget exhaustion is a *normal* state, not an error.

---

## 4. The Evaluation Engine — `evaluate.py`

**Decision (2026-07-27): ONE evaluation engine. Not one per market. Not one per timeframe.**

### 4.1 Rationale

The statistical layer — deflated Sharpe, White's Reality Check, CSCV/PBO, Monte Carlo, walk-forward — is market-agnostic mathematics. It must exist exactly once.

The reason is not code hygiene, it is **scientific validity**. The entire value of the Research Memory depends on A5 being able to compare experiment #6,201 in crypto against #12,483 in Indian equities. That comparison is only meaningful if both were scored by identical code. Fork `evaluate.py` six ways and within a year you have six subtly divergent PBO implementations, a Sharpe of 1.4 no longer means the same thing in two rows of the same table, and every cross-market lesson the knowledge graph produces is noise — silently.

### 4.2 The factoring

```
evaluate.py                 # single engine: orchestrates phases, computes all statistics
  ├── MarketProfile         # calendar, costs, constraints, benchmark, risk-free, currency
  ├── TimeframeProfile      # annualization, fill model, cost sweep, WF windows, min-N
  ├── validators/           # per-market data sanity checks (small, isolated, testable)
  └── gates/                # optional extra phases per market (see §4.6)
```

Two composable axes. Six markets × five timeframes = **30 profile files, not 30 engines.**

### 4.3 MarketProfile — what genuinely differs by market

These are *inputs*, never different algorithms:

| Field group | Contents |
|---|---|
| **Calendar** | Session hours, holidays, half-days. NSE 09:15–15:30; MCX to 23:30; crypto 24/7; forex 24/5 with Sunday open |
| **Costs** | Indian equities: brokerage + STT + stamp duty + exchange fee + GST. Crypto perps: maker/taker + **funding rate**. Forex: spread-driven. Futures: per-contract + roll cost |
| **Constraints** | Tick size, lot size, contract multiplier, margin, circuit limits (India) vs halts (US), short-selling rules (Indian cash equities: intraday only) |
| **Data hazards** | Survivorship & delisting (equities), contract roll/contango (commodities), exchange-specific bad prints (crypto) |
| **Reference** | Risk-free rate source, benchmark for alpha/beta, P&L currency, ADV/liquidity cap for capacity |

### 4.4 TimeframeProfile — what genuinely differs by timeframe

Different in kind, and the primary source of silent self-deception:

| Field | Why |
|---|---|
| **`periods_per_year`** | √252 daily vs √(252×375) for 1-minute. **Must come from the profile — never a hardcoded constant.** One wrong value makes every Sharpe in the database fiction |
| **`fill_model`** | Daily: next-open. Intraday: bar-level rules + spread-fraction slippage. Sub-minute: queue/latency assumptions we do not yet trust |
| **`cost_stress_multipliers`** | At 1-min, costs decide everything; at daily they are a rounding error. Intraday must be stressed at 2–5× assumed costs |
| **Walk-forward windows** | Sized in **bars** for statistical power *and* in **calendar time** for regime coverage. Both constraints must be satisfied |
| **`min_trades`** | Ties directly to the promotion rule (PRD §9.3) |
| **Overnight handling** | Gap risk, carry, crypto funding accrual — applies only if positions cross sessions |

### 4.5 Profile shape (illustrative)

```yaml
market: nse_equity
  calendar: nse
  costs:       {brokerage_bps: 3, stt_bps: 10, stamp_bps: 1.5, gst_pct: 18}
  constraints: {tick: 0.05, short_intraday_only: true, circuit_pct: 20}
  benchmark:   NIFTY50
  risk_free:   india_tbill_91d
  currency:    INR

timeframe: 15min
  periods_per_year: 6300
  fill_model: next_bar_open_with_spread
  cost_stress_multipliers: [1, 2, 3, 5]
  min_trades: 500
  walk_forward: {train_bars: 12000, test_bars: 3000, min_calendar_months: 18}
  overnight: false
```

### 4.6 Legitimate per-market variation — extra gates, not extra engines

Additional **phases appended** to the standard sequence, so core metrics remain identical and comparable:

- **Crypto:** venue/exchange robustness (does the edge survive on a second exchange?), funding-cost sensitivity
- **Equities:** survivorship-bias check, capacity/ADV constraint
- **Commodities:** roll-method sensitivity (does the edge depend on how contracts were stitched?)
- **Forex:** session-of-day dependence, carry decomposition

### 4.7 Provenance — mandatory on every experiment

Every experiment row records:

```
eval_engine_version      # semver of evaluate.py
market_profile_hash      # content hash of the resolved profile
timeframe_profile_hash   # content hash of the resolved profile
wf_config_hash           # content hash of {scheme, train_years, test_years} — §4A.2f/g
code_version             # git commit of the strategy code
data_snapshot_id         # exact dataset version used
operator_library_version
```

**Why:** the day cost assumptions change — and they will — we must instantly answer *"which of my 40,000 stored results are still comparable?"* Without this, the knowledge base silently mixes results scored under different rules. That is precisely the rot that kills long-running research systems.

Changing any profile or the engine bumps a version. Old results are not deleted; they are marked **incomparable** to the new version and optionally queued for re-evaluation.

---

## 4A. The Honest Score — our `val_bpb` ✔ RESOLVED

> ### The decision
>
> ```
> score = SR_oos  −  2 × SE(SR)  −  SR*(N_trials)
> ```
>
> **The deflated lower bound on out-of-sample Sharpe.** In words: *what Sharpe can we be confident is real, after accounting for how few trades we have, how ugly the tails are, and how many things we already tried?*
>
> `evaluate.py` returns **one float**. That float alone drives keep/discard. All other metrics are still computed and stored — they inform promotion and A3's reasoning, but they do not drive the loop.

Running every test in `evaluate.py` does not produce a score. Thirty metrics is a *report*; it cannot answer "is experiment 47 better than 46?" The score is a decision rule, chosen deliberately.

### 4A.1 Why this is the crux

The reference project works because it has **one honest scalar**. `val_bpb` is held out, vocab-independent (so architectures compare fairly), a single number, and effectively impossible to game.

The current design has a battery of ten tests and a vaguely-defined `primary_score`. Ten tests is a **report**. A loop needs a **decision**.

### 4A.2 Requirements

| `val_bpb` property | Required AQRL equivalent |
|---|---|
| Held out | Computed only on data the search never touched |
| Vocab-independent | **Frequency-fair** — a 5-trades/year and a 50-trades/day strategy must be comparable |
| Single scalar | One number, so "better or worse" is unambiguous |
| Hard to game | **Not raw Sharpe** — inflatable via leverage and frequency effects |
| — | Cost-inclusive by construction, not adjusted afterwards |

### 4A.2a The three terms

**Term 1 — `SR_oos`: Sharpe on data the search never fitted.**
Generated by **purged rolling walk-forward with an embargo gap** (§4A.2f): train on a window, test on the next, never overlapping, with a gap so nothing leaks across the boundary. All test windows concatenate into one return series (§4A.2h). **That series is the only thing ever scored** — never the fitted sample. The embargo gap must be **≥ the strategy's holding period**, or trades straddle the boundary and leak.

**Term 2 — the uncertainty haircut.** The standard asymptotic standard error of a Sharpe estimate:

```
SE(SR) = sqrt( (1 + SR²/2 − skew·SR + (kurtosis−3)/4 · SR²) / n )
```

This closes three attack vectors with one formula, without a rule for each:

| Gaming attempt | Why it fails automatically |
|---|---|
| Trade almost never (4 trades, all winners) | `n` small → error bar huge → lower bound collapses |
| Pick up pennies in front of a steamroller | negative skew → `−skew·SR` positive → SE grows → score drops |
| Rare catastrophic tail | kurtosis term grows → score drops |

**Term 3 — the trials haircut, `SR*(N_trials)`.** Try N strategies on pure noise and the best will show a respectable Sharpe by luck alone. That expected-best-under-null is computable from N and subtracted (the deflated Sharpe construction, Bailey & López de Prado). Try 10 things, subtract a little; try 10,000, subtract a lot. This is why `strategies.family` and the trial count exist in the schema.

### 4A.2b Costs are stressed by default

The OOS series is generated at **2× assumed costs**, not at face value. Cost stress is the default condition, not a separate later test.

### 4A.2c Why a bound and not a probability

The Probabilistic Sharpe Ratio returns a probability, which **saturates** — two good strategies both score 0.99 and the hill-climb loses its gradient. The loop needs a number that keeps moving so the agent can tell it is making progress, exactly as `val_bpb` does. A lower confidence bound provides that; a probability does not.

### 4A.2d Rejected alternatives

| Alternative | Why not |
|---|---|
| Raw Sharpe | Ignores selection, tails and sample size — this is the trap |
| Total return / CAGR | Leverage-gameable; ratios are not |
| Calmar (return / maxDD) | Max drawdown is one noisy worst-moment statistic. Fine as a gate, bad as a ranking |
| Probabilistic Sharpe | Saturates — see §4A.2c |
| A weighted blend of ten metrics | Every weight is a knob that gets tuned until the answer looks good. That is overfitting your own scorer |

### 4A.2e Frequency fairness

Annualising by `√periods_per_year` is fair **when trades are independent** — a higher-frequency strategy genuinely gets more independent observations, so a higher Sharpe is real rather than an artifact. The unfairness appears with **autocorrelated returns** (overlapping positions, slow-decaying signals), which require an autocorrelation correction or the Sharpe is inflated.

Capacity — which genuinely favours lower frequency — stays as the **secondary ranked criterion** (§4A.3), never blended into the primary score, so neither can hide the other.

### 4A.2f The walk-forward scheme ✔ DECIDED

The generator behind Term 1. Several schemes exist and **the choice between them is itself a way to fool yourself** (§4A.2g).

| Scheme | Shape | Notes |
|---|---|---|
| **Rolling / sliding** | train 2016 → test 2017; train 2017 → test 2018 … | Fixed train length, slides forward. Old data drops out |
| **Anchored / expanding** | train 2015–2020 → test 2021; train 2015–2021 → test 2022 … | Fixed start, growing train set |
| Single holdout | train 2000–2015 → test 2016–2025 | One fold. Simple, high variance |
| Combinatorial purged CV | many valid train/test combinations across N groups | Yields a *distribution* of outcomes. What PBO is built on. Expensive — deferred past Stage 0 |

**Decision: rolling window, 1-year test periods, purged with embargo ≥ holding period.**

Rationale: it matches how a swing strategy is actually maintained; 2000–2025 yields ~24 folds; train and test lengths stay constant so **folds are comparable to each other**. Anchored fails that last property — its later folds carry five times the training data of its early ones. Anchored is the better choice only when data is scarce, which at 25 years it is not.

> **Override condition:** if parameters will be fitted once and never revised in production, switch to anchored — the validation scheme should mirror how the strategy will actually be maintained. Changing this mid-campaign invalidates comparability (§4.7).

### 4A.2f-a Train window length ✔ DECIDED

**Test window is always 1 year (fixed, non-negotiable — §4A.2f). Train window length is configurable: 1, 2, or 3 years**, chosen once per strategy family before the campaign begins.

The train window is *not* a free variable to be swept in search of a better score — see §4A.2g. It is a modelling decision about how much history the strategy's slowest-moving component genuinely needs, made once, by the human, in advance.

**What actually trades off, precisely:**

- **Fit stability vs recency.** More train years gives more data to estimate parameters or statistics robustly — this matters if the strategy relies on slow-moving structure (long-lookback indicators, correlation regimes, macro conditioning). But a longer train window also anchors each fold's fit to older information relative to the 1-year test period that follows, so the strategy adapts more slowly if the underlying relationship drifts.
- **Fold count, and its effect on `n` is real but modest, not dominant.** Because the test window is always 1 year regardless of train length, total out-of-sample observations scale roughly as `(dataset_years − train_years)`. On ~26 years of data: train=1yr → ~25 years of OOS data; train=3yr → ~23 years. An ~8% difference in `n` — worth knowing, but the fit-stability-vs-recency tradeoff above is the dominant consideration, not the width of `SE(SR)`.
- **The test window never changes with train length.** It stays fixed at 1 year because it represents the strategy's realistic re-fit/re-validation cadence in production (§4A.2f), which is independent of how much history each fit is allowed to see.

**Default, absent a specific reason otherwise: 1 year.** Maximises fold count and forces the strategy to prove it doesn't need a long memory to work. Move to 2 or 3 years only when the strategy's own logic demands more history to stabilise — and say so in the campaign record.

**An emergent property worth naming:** because `evaluate.py` is neither readable nor writable by the agent (§2A.3), the walk-forward configuration — scheme, train length, test length — **cannot be an iteration lever at all.** A3 may propose changes to `strategy.py` (§6.1 of the PRD), but it can never propose "try a 2-year training window instead" — that would be a change to the evaluation contract, which is structurally outside its reach. This is not a limitation to work around; it is the correct consequence of evaluator isolation, and it is exactly why the choice is safe to leave with the human.

### 4A.2g Scheme selection is a hidden multiple-testing channel ★

Run rolling, get 0.4. Run anchored, get 0.9. Report 0.9. **The deflated Sharpe will not catch this**, because `N_trials` counts strategies tried, not validation methods tried. The leak sits entirely outside the integrity machinery. **The same leak applies to train window length** — running train=1yr, train=2yr and train=3yr and reporting whichever scored best is the identical mistake in a different variable.

Therefore:

- **One scheme, and one train window length, per campaign** — both fixed in `evaluate.py` before searching begins.
- The agent cannot select either — `evaluate.py` is unreadable and unwritable (§2A.3).
- Both are **hashed into provenance** alongside the profiles, as `wf_config_hash` (§4.7). Changing either marks all prior results `comparable = 0`.
- If a human deliberately wants to compare train lengths, that is a legitimate research question — but it must be run as **separate, explicitly labelled campaigns**, each contributing its own count to `N_trials`, never as a silent retry.

### 4A.2h Combining folds ✔ DECIDED — concatenate, always

**The score is computed from a single concatenated series.** All test-window returns are joined end to end into one out-of-sample track record, and `SR`, `skew`, `kurtosis` and `n` are computed once over that series.

The alternative — scoring each fold and averaging — is **not** used for the score.

Per-fold metrics are still **computed and stored** (`fold_metrics`, `folds_profitable`, `wf_efficiency`) because they diagnose something concatenation hides: a strategy brilliant in 3 folds and terrible in 5 can still concatenate to a respectable Sharpe. They inform promotion review and A3's reasoning; they do not drive keep/discard.

**Walk-forward efficiency** (out-of-sample ÷ in-sample performance) is the key diagnostic — if OOS is far below IS, each fold is overfitting internally even though the concatenated series looks acceptable.

### 4A.2i What walk-forward actually tests

Walk-forward validates the **fitting process**, not the strategy. Each fold re-runs parameter selection on the training window alone and checks whether the result survives the next period.

This has a sharp consequence:

- **If the agent tunes parameters** → each fold must re-run that tuning from scratch, on training data only. If tuning ever touches the test window, the entire exercise is theatre.
- **If the agent hardcodes parameters** → walk-forward is not testing a fitting procedure at all; it is testing robustness across time periods. Still useful, but it is not overfitting protection, and the trials haircut carries correspondingly more weight.

`evaluate.py` must be built for whichever case applies. See §14 open questions.

### 4A.3 Ranked criteria

Mirrors the reference's primary/secondary/tertiary structure (PRD §13.3): **primary** the honest score; **secondary** a resource constraint (capacity or turnover — the analogue of his VRAM ceiling); **tertiary** simplicity, scored rather than left to reviewer judgment.

### 4A.3a The bar and the score are separate ★

A hard pass/fail bar runs **before** any score is computed. This is what resolves the drawdown question.

| The bar (pass/fail, pre-registered) | The score (ranking, hill-climbing) |
|---|---|
| Minimum trade count | `SR_oos − 2·SE(SR) − SR*(N)` |
| **Maximum out-of-sample drawdown** | |
| Minimum breadth across instruments | |
| Profitable at 2× costs | |
| Maximum complexity (rules / free parameters) | |

Fail any bar item → **`discard`, no score computed, stop.**

**Drawdown deliberately does not enter the score.** Max drawdown is a single worst-moment statistic — very noisy, highly dependent on the sample window. Ranking on it means ranking partly on luck. As a *gate* its noisiness is harmless; as a *ranking* it is corrosive.

### 4A.3b Gates are enforced in `evaluate.py`, not only `program.md` ★

`program.md` is *instructions to the agent* — the agent decides whether it complied, and will eventually persuade itself that 40 trades is close enough to 100.

The bar therefore lives in **both files with different jobs**:

- **`program.md`** — states the bar so the agent knows what it is aiming at
- **`evaluate.py`** — **enforces** it, returning `discard` and no score

Since the agent can neither read nor edit `evaluate.py` (§2A.3), the gate is a fact rather than a request. Structural, not procedural — the same principle as hiding the scorer.

### 4A.3c Out-of-sample data is a consumable resource

Every iteration against the walk-forward OOS window makes that window slightly less out-of-sample. After a few thousand iterations it is effectively in-sample — it has simply been fitted more slowly.

The trials haircut compensates mathematically and the vault (§8A.2) protects the final promotion decision, but the real defence is **satisficing** (PRD §13.2). The loop stops at the first strategy clearing the bar not out of modesty, but because every extra iteration spends a resource that cannot be refilled.

### 4A.4 The invariant

The reference achieves comparability with a single constant — the 5-minute wall clock — rather than a versioning scheme. The AQRL analogue is a **fixed evaluation contract**: data slice, cost model, and test protocol held constant across a campaign. §4.7 provenance hashing enforces this; the design goal is to keep the contract simple enough that it rarely changes, because every change partitions the result history.

---

## 4B. Performance & Parallelism

**Speed is a first-class requirement, because throughput is what makes the loop viable at all.**

### 4B.1 Why it decides whether the lab works

| `evaluate.py` runtime | Experiments overnight (12h, 1 core) |
|---|---|
| 1 second | ~43,000 |
| 10 seconds | ~4,300 |
| 2 minutes | ~360 |
| 20 minutes | ~36 |

That is the difference between a research laboratory and a slow notebook. The reference project (PRD §14) fixes a 5-minute budget precisely so ~100 experiments fit in a night. **Target: `evaluate.py` completes in seconds, not minutes.**

### 4B.2 Vectorise the maths, JIT the path

- **Vectorised NumPy / Polars for everything expressible as array maths** — indicators, transforms, returns, aggregations. No Python loops over bars.
- **Numba JIT for genuinely path-dependent logic** — trailing stops, position state, sequential fills. These cannot be vectorised honestly, and a `@njit` loop is far faster than a Python one *and* far easier to keep correct than a contorted vectorised version.
- **Polars over pandas** for large frames; **DuckDB** for analytical queries straight over Parquet without materialising in Python.
- **Memory-mapped columnar reads.** Load only the columns and date range a fold needs.
- **Compute indicators once per snapshot, not once per fold.** Across ~24 rolling folds this is the single largest easy win.

### 4B.3 Parallelism — processes, not threads ★

**Python threads do not speed up CPU-bound backtesting.** The GIL serialises them; you get complexity and no throughput. Use:

- **`multiprocessing` / `joblib` across independent units of work**
- **NumPy and Numba release the GIL** internally, so vectorised work already uses hardware efficiently within one process
- Free-threaded CPython builds are maturing but should not be depended on

What is embarrassingly parallel, in priority order:

| Work | Parallel across | Notes |
|---|---|---|
| Walk-forward folds | ~24 folds | Each fold is fully independent — the biggest single win |
| Monte Carlo / bootstrap | replications | Trivially parallel |
| **Null-world calibration** | replications × null models | The heaviest job in the system; parallelise hard |
| Parameter sweeps | combinations | |
| Multiple experiments | strategies | Later stages, once the queue exists |

Rule of thumb: parallelise at the **outermost independent level** (folds, replications), not inside the inner maths — the inner loop should already be vectorised or JIT-compiled.

### 4B.4 The tension nobody mentions: vectorisation is the top source of look-ahead ★

This is the one place where "make it fast" fights "make it honest," and speed must not win.

Classic vectorised leaks:

```python
df['signal'] = df['close'] > df['ma']          # signal from THIS bar's close
df['ret'] = df['signal'] * df['close'].pct_change()   # ...traded at THIS bar's close
```

Others: rolling z-scores or percentile ranks computed over the **whole** series; `fillna(method='bfill')` pulling values backwards from the future; centred rolling windows; any normalisation fitted on the full sample before splitting.

Consequences for the design:

- **P0's look-ahead checks (§5.1) become more important as the code gets more vectorised**, not less.
- Every signal must be explicitly lagged relative to the bar it can act on, and that lag verified by test, not by eyeballing.
- **Known-answer tests** (Implementation_Plan §4.2) must include a deliberately leaky vectorised strategy that P0 is required to catch.
- **The prohibitions are written into `program.md`** (§2A.3a) so the agent avoids them by default — but that is error reduction, not enforcement. P0 remains the guard.

### 4B.5 Determinism under parallelism — non-negotiable

Parallel execution must not change results. Reproducibility is a hard requirement (§13), and a score that shifts between runs destroys the comparability everything else rests on.

- **Seeds derived per fold / per replication** from a base seed, never from wall clock or worker ID.
- **Deterministic reduction order** — floating-point summation is not associative, so results must be combined in a fixed order regardless of which worker finishes first.
- Concatenation of fold returns follows **chronological order**, never completion order.
- The same experiment re-run must produce a **bit-identical** `honest_score`.

### 4B.6 Per-experiment time budget

Borrowed from the reference project's fixed wall clock: an experiment exceeding its budget is **killed and recorded as `crash`**, not allowed to run for an hour. This keeps overnight throughput predictable and stops one pathological strategy from consuming a whole night.

### 4B.7 Order of work

**Correct first, then measure, then optimise the measured bottleneck.** A fast wrong answer is worse than a slow one, because it is wrong at scale. But nothing in the architecture may *preclude* speed — hence vectorised data structures, process-level parallelism, and columnar storage from the start.

Profile before optimising. The bottleneck is rarely where it feels like it is; on this workload it is usually data loading and per-fold indicator recomputation, not the maths.

---

## 5. The Validation Battery

Executed as an ordered funnel. Cheap tests first; a failure short-circuits the rest.

| Phase | Name | Contents | Cost |
|---|---|---|---|
| **P0** | Smoke | Code compiles, runs, produces trades. **Look-ahead & leakage static checks.** No NaN/inf. Trade count > 0 | Seconds |
| **P1** | Fast backtest | Small data slice, frictionless. Sanity: is there any signal at all? | Seconds–minutes |
| **P2** | Full backtest | Complete history, realistic costs and fills, correct calendar | Minutes |
| **P3** | Robustness battery | Walk-forward, Monte Carlo, deflated Sharpe, White's Reality Check, CSCV/PBO, regime analysis, cost sensitivity, parameter sensitivity | Minutes–hours |
| **P4** | Paper trading | Forward evidence in live market conditions | Weeks–months |

### 5.1 Phase 0 is the most underrated

Look-ahead bias and data leakage are the dominant failure modes of LLM-written strategy code. P0 must include automated checks:

- No use of future bars in signal computation (shift/lag verification)
- No fitting on the full sample before splitting
- No survivorship-biased universe construction
- No use of point-in-time-unavailable fundamentals
- Signal→order→fill ordering respects the fill model

A strategy that fails P0 is a **bug**, routed back to A2 with the diagnostic — it is not a research finding and must not pollute the knowledge base as one.

### 5.2 Multiple-testing discipline

Non-negotiable and easy to get wrong. The deflated Sharpe ratio requires the **number of trials**. That number must include:

- Every iteration of the A2↔A3 loop for this strategy
- Every parameter combination swept
- Historically, the related experiments already run in the same family

Under-counting trials makes deflated Sharpe a rubber stamp. The experiment DB must be able to answer "how many trials have been conducted in this family?" — this is a hard schema requirement, not an afterthought.

### 5.3 Gate outcomes

Each phase yields `PASS | FAIL | WARN` per test plus a numeric score. Thresholds are configuration, versioned alongside profiles. Gating vs advisory classification is an open question (PRD §13).

---

## 6. The Operator Library

**The LLM does not invent arbitrary formulas.** It composes from a vetted, versioned library. This shrinks the search space enormously, makes strategies interpretable and reviewable, and makes results comparable across experiments.

| Category | Examples |
|---|---|
| **Transformations** | Rolling mean, EMA, JMA, Kalman filter, ATR normalization, wavelets, PCA, fractional differencing |
| **Signal operators** | Crossovers, thresholds, breakouts, volatility expansion, momentum, mean reversion, volume confirmation |
| **Risk operators** | ATR stop, time stop, trailing stop, position sizing, Kelly variants, volatility targeting |
| **Portfolio operators** | Risk parity, equal weight, correlation clustering |

### 6.1 Requirements

- Every operator is versioned, unit-tested, and documented with parameter ranges.
- Every operator declares which timeframes and markets it is valid for.
- A strategy spec is a **composition of operators**, expressible as a DAG — this makes specs diffable, hashable, and searchable for near-duplicates.
- **Duplicate detection:** before implementing, the spec's canonical hash is checked against all prior specs. Re-running an identical composition is forbidden; near-duplicates surface the prior result to A3.
- New operators may be proposed (by A1 from literature, or by the human) but enter the library only via an explicit review step with tests. The library is not self-modifying without a gate.

---

## 7. Knowledge Subsystem

### 7.1 Internal memory

Append-only. See `Backend-Schema.md` for tables. Key requirement: **every experiment must be reproducible from its stored record alone** — spec, code version, data snapshot, profiles, seeds.

### 7.2 External ingestion pipeline

```
Internet
   │
   ▼
Collectors  (Python, scheduled — arXiv, SSRN, GitHub, blogs, market data)
   │
   ▼
Cleaning & deduplication
   │
   ▼
Knowledge extraction  (LLM, once per document, ever)
   │
   ▼
External Knowledge Base  (structured records + embeddings)
   │
   ▼
Consumed by A1
```

**Claude is not a crawler.** Collectors produce raw artifacts; a single extraction pass converts each to a structured record; the raw document is archived and never re-read.

### 7.3 Curiosity queue

Failure patterns and A3/A5 observations generate **research questions** with a topic, motivation, and originating experiment. Collectors consult this queue to run targeted searches rather than only broad sweeps. Questions carry a priority and a status so the loop can be audited: *did asking this question ever produce a usable hypothesis?*

### 7.4 Knowledge graph

Beyond flat storage, A5 maintains a graph of concepts and conditional relationships:

```
Momentum ──works-in──> Trending, Low-Volatility
         ──fails-in──> Sideways, High-Volatility
JMA      ──pairs-well-with──> ATR
         ──pairs-poorly-with──> RSI
         ──effective-in──> Commodities
         ──weak-in──> Forex
```

Edges carry **evidence counts and confidence**, and link back to the experiments that support them. An edge with no experiment backing it must not exist.

---

## 8. Data Layer

| Store | Purpose |
|---|---|
| **SQLite** (v1) → **PostgreSQL** (v3) | Experiment metadata, jobs, state machine, knowledge entries |
| **Parquet** | Market data, tradebooks, equity curves, per-trade records |
| **DuckDB** | Analytical queries over Parquet without loading into Python |
| **Vector index** | Embeddings for literature and prior-experiment similarity search |
| **Object storage** (v3) | Raw documents, large artifacts, archived runs |

### 8.1 Market data requirements

- Immutable, versioned **snapshots**. An experiment references a snapshot ID, never "whatever was on disk that day."
- Point-in-time correctness: no restated data leaking backward.
- Per-market validators run at ingest, not at experiment time.
- Adjustments (splits/dividends) recorded as a versioned method, since the choice affects results.

---

## 8A. Adversarial Integrity & Self-Calibration ★

The three mechanisms that make automated search in markets defensible. **None of these are optional, and all three precede any real-data result.**

### 8A.1 Reward hacking is a certainty, not a risk

Give an agent a scoring function and enough iterations and it will optimise the scorer rather than the market — usually by accident, through a subtle look-ahead path, a fill assumption, or a near-zero denominator.

Defences:

- **`evaluate.py` is neither readable nor writable by the agent** (§2A.3). Separate process, no source access.
- **`data.py` is read-only.** An agent able to edit the cost model will eventually make costs cheaper and call it a discovery.
- **"Too good to be true" tripwire.** Sharpe > 3 on daily data is a *bug hypothesis*, not a discovery. Auto-route to adversarial audit rather than promotion.
- **Periodic red-teaming.** Deliberately task an agent with breaking `evaluate.py`; treat every exploit found as a high-value knowledge entry and fix it.

### 8A.2 The vault — data the loop cannot read

Every other protection — deflated Sharpe, walk-forward, PBO — depends on honestly counting trials. Once an LLM generates hypotheses influenced by a memory of past results, the effective trial count becomes genuinely unknowable. The vault is the one defence that does not depend on counting anything.

- A span of years, and/or a set of instruments, and/or an entire market is **locked away**.
- The research loop has **no read path**. Not "should not" — *cannot*.
- Opened only at promotion, **once per strategy family**.
- Every open is logged (`vault_access_log`) and counts against a lifetime budget.
- A family that exhausts its budget cannot be promoted again until genuinely new data exists.

### 8A.3 Null-world calibration — measuring our own false discovery rate

The procedure defined in PRD §4.5, stated as an engineering requirement:

- Generate datasets with **no alpha by construction**: permuted returns, block bootstrap, synthetic paths with matched volatility and fat tails.
- Run the **complete loop** — generation, iteration, evaluation, promotion recommendation — against them.
- Count reported discoveries. That count is the false discovery rate.

Requirements:
- Runs as a **permanent regression test** after any change to `evaluate.py`, the scoring rule, or any profile.
- Results recorded in `null_world_runs` and surfaced on the Laboratory screen beside cost-per-discovery.
- **Milestone 0.** No real-data result is trusted before FDR has been measured and driven low.

### 8A.4 The autonomy ratchet

Experiment throughput is tied to measured FDR. If FDR rises, throughput automatically drops. Scaling becomes earned rather than assumed — the reference project's encouragement toward ~100 experiments overnight is safe only once the pipeline has demonstrated it does not invent discoveries at that volume.

---

## 9. LLM Integration Requirements

- **Model:** Claude, via Claude Code sessions. Sessions are stateless and disposable.
- **Context assembly is a Python responsibility.** The worker builds the prompt — relevant knowledge entries, prior iterations, evaluation report, operator catalog — and hands Claude a complete brief. Claude does not go hunting for context.
- **Structured outputs.** Every agent returns schema-validated JSON. Free-text goes in dedicated reasoning fields, never mixed with machine-read values.
- **Prompt versioning.** Prompts are versioned artifacts stored in the repo; the prompt version is recorded on every agent output. A prompt change is a system change and affects comparability.
- **Determinism where possible.** Seeds recorded. Where the LLM is inherently non-deterministic, the *output artifact* is stored so the experiment remains reproducible even if regeneration would differ.
- **Cost accounting.** Tokens and dollars recorded per job, per strategy, per discovery.

---

## 10. Observability & Audit

- **Structured logs** for every job with correlation IDs threading `strategy → experiment → job`.
- **The "why" record.** Every agent decision stores its reasoning and the evidence it cited. This is a hard requirement (PRD §10.1), not a nice-to-have.
- **Lab notebook per experiment**, auto-generated:
  ```
  Experiment #12,483
  Hypothesis:  Adaptive ATR works better in volatile markets
  Result:      Rejected
  Reason:      Overfit to 2019–2021
  Evidence:    Sharpe collapsed 2.4 → 0.6 out-of-sample; PBO 0.71
  Confidence:  94%
  Next:        1. Normalize ATR  2. Try volatility clustering  3. Test on commodities
  ```
  The "Next" section is mandatory — it is what makes the system self-propelling.
- **Metrics dashboard** tracking the PRD §4.3 funnel numbers.

---

## 11. Safety & Risk Controls

- **Two mandatory human gates:** research→paper, and paper→live. No code path may bypass them.
- **The agents have no trading credentials.** Order placement is a separate, minimally-scoped service. An agent can *recommend*; only the execution service, gated on a human-approved record, can act.
- **Hard risk limits** enforced outside the strategy logic: per-strategy max loss, per-portfolio max drawdown, position limits, kill switch. These must make a 100% drawdown structurally unreachable.
- **Live capital ramps** in stages (1–5% → scale up), never straight to full allocation.
- **Rollback:** any promoted strategy can be demoted or halted from the dashboard immediately.
- **Sandboxed code execution.** A2 writes code that will be executed; it runs in an isolated environment with no network and no credentials.

---

## 12. Technology Choices

| Concern | v1 | Later |
|---|---|---|
| Language | Python 3.11+ | same |
| Array maths | NumPy + Polars (Parquet-native) | same |
| Path-dependent loops | Numba `@njit` | same |
| Analytics over Parquet | DuckDB | same |
| Parallelism | `multiprocessing`/`joblib` across folds and replications — **not threads** (§4B.3) | Distributed workers |
| Reasoning | Claude Code (stateless sessions) | same |
| Strategy code history | git (hash referenced from SQLite) | same |
| Job queue | none in nanoAQRL; SQLite table + leases when the scheduler arrives | Redis |
| Metadata DB | SQLite, 3 tables to start (§2A.5) | PostgreSQL |
| Columnar data | Parquet + DuckDB | same + object storage |
| Vector search | Local (FAISS/sqlite-vss) | Dedicated vector DB |
| Scheduling | `scheduler.py` tick loop | Prefect/Airflow if warranted |
| Isolation | subprocess | Docker → Kubernetes |
| Dashboard | Local web app | same, hosted |

Deliberately boring. The novelty budget is spent on the research loop, not the infrastructure.

---

## 13. Non-Functional Requirements

| Requirement | Target |
|---|---|
| **`evaluate.py` runtime** | **Seconds, not minutes** (§4B.1) |
| **Determinism under parallelism** | Bit-identical `honest_score` on re-run (§4B.5) |
| **Fold-level parallelism** | Scales with cores; no shared mutable state |
| Experiment reproducibility | 100% from stored record |
| Scheduler recovery | Resumes cleanly after kill -9; no orphaned RUNNING jobs beyond lease TTL |
| Idempotency | Re-running any job produces no duplicate state |
| Backwards compatibility | Old experiments remain readable after schema migration |
| Laptop viability | Full v1 loop runs on a single consumer laptop |
| Horizontal scale | Adding workers requires zero agent-logic changes |

---

## 14. Open Technical Questions

- [x] ~~The honest score~~ — **resolved, §4A**
- [ ] Confidence level on the uncertainty haircut: `2×SE` (~97.5% one-sided) vs `1.65×SE` (~95%, more candidates survive). *Owner: human*
- [ ] Numeric bar values — min trades, max OOS drawdown, breadth, complexity cap. *Owner: human, written into `program.md` and enforced in `evaluate.py`*
- [ ] Autocorrelation correction — required from the start, or only once overlapping-position strategies appear?
- [ ] **Does the agent tune parameters per fold, or write fixed-parameter strategies?** Determines what walk-forward is actually testing and how `evaluate.py` is built (§4A.2i). *Owner: human*
- [x] ~~Rolling train-window length~~ — **resolved: configurable 1/2/3 years, test fixed at 1 year, default 1 year (§4A.2f-a)**. Which of the three for the first campaign is still *Owner: human*
- [ ] Minimum fold count before a score is considered meaningful
- [ ] Null-world generator: which null models, and how many replications for a stable FDR estimate?
- [ ] Vault composition — which years, instruments, or markets are locked, and what is the per-family peek budget?
- [ ] How is `evaluate.py` isolated in practice so the agent cannot read it (separate process, container, or file permissions)?
- [x] ~~Walk-forward window sizing policy — fixed, expanding, or anchored?~~ — **resolved: rolling, 1-year test windows (§4A.2f)**
- [ ] Which Monte Carlo variant is canonical (trade-order shuffle, block bootstrap, synthetic path generation)?
- [ ] Trial-counting scope for deflated Sharpe — per strategy, per family, or global?
- [ ] Near-duplicate spec detection: exact hash only, or embedding similarity threshold?
- [ ] Vector index choice for v1
- [ ] How is the operator library versioned against in-flight experiments?
- [ ] Paper trading: simulated internally vs broker paper API per market?

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial document. Execution model, single-`evaluate.py` decision with Market/Timeframe profile factoring, provenance hashing, validation battery, operator library, knowledge subsystem, safety controls. |
| 2026-07-27 | Added §2A nanoAQRL (the actual v1 shape, file permissions, SQLite+git split, 3-table minimum), §4A the honest score and ranked criteria, §8A adversarial integrity (reward hacking, the vault, null-world calibration, autonomy ratchet). Evaluator is now unreadable as well as unwritable by the agent. |
| 2026-07-27 | Added **§2A.3a — required contents of `program.md`**: the reveal/hide split (correctness rules are shown since they are not gameable; the scoring formula and bar numbers are hidden since they are), the full anti-look-ahead rule set the agent must follow, and behavioural rules including "a P0 rejection is a bug, not an obstacle". Instructions reduce the error rate; P0 still enforces. |
| 2026-07-27 | Added **§4B Performance & Parallelism** — throughput targets, vectorise-the-maths/JIT-the-path, process-level parallelism across folds and replications (threads are useless here under the GIL), determinism requirements under parallelism, per-experiment time budget, and the tension that vectorisation is the top source of look-ahead bias. |
| 2026-07-27 | Added **§4A.2f-a — train window length.** Test window stays fixed at 1 year; train window is configurable at 1/2/3 years, chosen once per family before the campaign, defaulting to 1 year. Documented the real tradeoff (fit stability vs recency; a modest ~8% effect on total OOS `n`, not the dominant factor). Extended §4A.2g: train length is subject to the same hidden-multiple-testing risk as scheme choice, so it is fixed per campaign and folded into a new `wf_config_hash` alongside the scheme. Noted the emergent property that evaluator isolation makes the walk-forward configuration structurally impossible for any agent to select — it can never be an iteration lever. |
| 2026-07-27 | **Walk-forward resolved.** Scheme fixed as rolling with 1-year test windows (§4A.2f); fold combination fixed as concatenation into a single OOS series (§4A.2h), with per-fold metrics stored for diagnosis but not driving keep/discard. Added §4A.2g — scheme selection is a multiple-testing channel the deflated Sharpe cannot see, so the scheme is fixed per campaign and hashed into provenance. Added §4A.2i on what walk-forward actually tests. |
| 2026-07-27 | **§4A resolved.** Honest score fixed as the deflated lower bound on out-of-sample Sharpe: `SR_oos − 2·SE(SR) − SR*(N_trials)`, on purged/embargoed walk-forward returns at 2× costs. Added the three-term derivation and the gaming vectors each term closes, rejected alternatives, bar/score separation (drawdown gates but does not rank), gate enforcement in `evaluate.py` rather than `program.md` alone, and OOS as a consumable resource. |
