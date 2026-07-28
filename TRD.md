# TRD — AQRL Technical Requirements

> **Status:** Design complete for v1. No implementation started.
> **Last updated:** 2026-07-28
> **Companion docs:** `PRD.md` (why) · `Backend-Schema.md` (data) · `App-Flow.md` (sequences) · `Implementation_Plan.md` (build order)

---

## 1. Architectural Stance

AQRL is **not an "AI agent" project.** It is a distributed research system that happens to use an LLM for the reasoning steps. The mental model is a job queue with workers, not a chatbot with tools.

Three rules follow:

1. **Claude only thinks.** Collectors, schedulers, databases, backtests and statistics are ordinary Python. Claude must never spend tokens downloading a PDF or renaming a file.
2. **Claude is stateless.** One session per job, then destroyed. The database holds all state. This makes the system restartable, parallelisable and debuggable.
3. **Agents know queues, not each other.** No agent calls another agent. They read and write rows; the scheduler notices and dispatches. This is what makes 1 worker and 100 workers the same code.

### 1.1 The four components

| Component | Role | Implementation |
|---|---|---|
| **Brain** | Reasoning | Claude Code sessions, one per job |
| **Hands** | Execution | Backtest engine, statistical battery, data layer |
| **Memory** | Accumulated knowledge | Experiment DB + knowledge graph + vector index |
| **Nervous System** | Coordination | Scheduler, job queue, state machine, budget control |

The Nervous System is the piece most projects omit. Without it you have a collection of scripts; with it you have a laboratory.

---

## 2. What v1 Actually Is — nanoAQRL ★

**Everything in §3 is the destination. This section is the starting point.**

The reference project (PRD §13) runs a complete autonomous research loop in three files. v1 mirrors that shape. The five-agent architecture, job queue, knowledge graph and full schema are **later stages**, added when a felt need arrives — never built up front.

### 2.1 The five files

| File | Contents | Agent permission |
|---|---|---|
| `data.py` | Snapshots, calendars, cost models, universe definition | **read only** |
| `strategy.py` | Signal logic, entries, exits, filters, sizing | **the only writable file** |
| `evaluate.py` | The scoring harness and the hard bar | **neither readable nor writable** |
| `program.md` | Operating instructions and the acceptance bar | **human-edited only** |
| `results.tsv` | `commit \| score \| n_trades \| status \| description` | append only |

### 2.2 The loop

```
edit strategy.py → commit → run evaluate.py
      → bar failed?    discard, no score computed
      → bar cleared?   keep the commit — and STOP (PRD §9.2)
      → append one row to results.tsv
      → repeat, unattended
```

Status values are exactly three: `keep` · `discard` · `crash`. Every experiment gets one — forcing a verdict prevents results piling up unjudged.

### 2.3 One deliberate deviation from the reference

The reference lets the agent **read** `prepare.py`; only editing is forbidden. **We forbid reading `evaluate.py` as well.**

`val_bpb` survives being understood — knowing how a held-out likelihood is computed does not help you fake one. **A backtest score does not survive being understood.** An agent that can read the scorer will eventually exploit a weakness in it, not from malice but because exploiting the measurement is the cheapest path to a higher number (§14.1).

### 2.4 What `program.md` must contain ★

Because the agent cannot read `evaluate.py`, `program.md` is the **only** channel through which it learns what will be checked. That forces a deliberate split:

| Reveal | Hide |
|---|---|
| **Correctness rules** (below). Not gameable — hiding them only causes avoidable failures | **The scoring formula and its thresholds.** These *are* gameable — an agent that knows the exact haircut can aim at it |
| The bar's *dimensions* (trades, drawdown, breadth, complexity exist) | The bar's *numbers* |

**These instructions reduce the error rate. They enforce nothing.** The agent cannot grade its own look-ahead; P0 (§10.1) remains the enforcement layer. Both, always.

#### Required — anti-look-ahead rules

**Signal timing**
- Every signal is computed from data available at or before bar *t*, and acted on at *t+1* or later per the fill model.
- Explicitly lag every signal before combining it with returns. Never multiply a same-bar signal by a same-bar return.
- `shift(-n)` is forbidden anywhere, for any reason.

**Statistics and normalisation**
- Rolling statistics only. Never compute mean, standard deviation, z-score or percentile rank over the whole series.
- No centred windows (`center=True`).
- Any fitted transform — scaler, PCA, threshold — is fitted on **training data only** and applied to test data.

**Missing data**
- `bfill()` / `fillna(method='backfill')` is forbidden — it pulls the future backwards.
- Forward-fill only, and be explicit about why it is safe.

**Resampling and joins**
- A bar's own close is not known until that bar closes. Do not use it to decide an action inside the same bar.
- Timestamp merges must not silently align future data to past rows.

**Fitting**
- Parameter tuning happens **inside the fold, on the training window only** (§8.5). If tuning ever touches the test window, the fold is worthless.

**Verification duty**
- Before committing, state in the commit message which bars each signal reads and what its lag is. Articulating the timing catches most errors before they reach P0.

#### Required — behavioural rules

- **A P0 rejection is a bug in your code, not an obstacle.** Fix the cause. Do not restructure code to pass the check while preserving the behaviour — that is precisely the failure mode this architecture exists to prevent.
- **Prefer the simpler strategy** where results are close (tertiary criterion, §7.6).
- **Stop when the bar is cleared.** Do not keep searching for a higher number.
- **Do not pause to ask the human whether to continue.**

### 2.5 Minimum viable schema

`Backend-Schema.md` defines 15+ tables. Building all of them before running one experiment is designing the archive before doing the science. v1 starts with **three**:

- `strategies` — the research thread, carrying `family` for trial counting
- `experiments` — one row per attempt: provenance, `code_commit`, status
- `evaluations` — the metrics from `evaluate.py`

`jobs` arrives with the scheduler. Everything else is added on felt need; the designs already exist, so later addition is cheap.

---

## 3. Runtime Topology & Scaling Path

### 3.1 The eventual v1 shape (post-nanoAQRL)

Exactly **one always-running process**: `scheduler.py`. Everything else is invoked on demand.

```
scheduler.py  (tick loop, ~60s)
      ├─ reads jobs table (SQLite)
      ├─ dispatches ready jobs to workers
      ├─ enforces budgets & concurrency caps
      └─ advances the state machine

workers (subprocess, on demand)
      ├─ agent1_research.py    → Claude session
      ├─ agent2_engineer.py    → Claude session
      ├─ agent3_reviewer.py    → Claude session
      ├─ agent4_promotion.py   → Claude session
      ├─ agent5_knowledge.py   → Claude session
      ├─ librarian.py          → Claude session
      ├─ evaluate.py           → pure Python, no LLM
      └─ collectors/*.py       → pure Python, no LLM
```

A single scheduler is dramatically easier to debug than six independent daemons. This is deliberate.

### 3.2 Scaling path — do not build yet, do not preclude

| Phase | Change |
|---|---|
| **v1** | SQLite, one scheduler, sequential execution, one Claude session per job |
| **v2** | Multiple A2/evaluate workers in parallel, Redis job queue, Docker isolation |
| **v3** | Multiple machines or cloud VMs, PostgreSQL, shared object storage, Kubernetes |

**The v1→v3 migration must be a swap of the queue and DB backends only.** Nothing in agent logic may assume SQLite or single-machine execution: no file locks as coordination, no in-process shared state, all job claims via atomic DB transactions with a lease and heartbeat.

### 3.3 Eventual target hardware — reference only

Documented so the architecture does not preclude it:

- **Control plane** (24/7): 8–16 vCPU, 32–64 GB RAM, 1 TB SSD — scheduler, Postgres, vector DB, job queue, dashboard API. No GPU.
- **Research nodes** (CPU cluster): ~20 × (32 vCPU, 128 GB RAM, 2 TB NVMe) — backtests, walk-forward, Monte Carlo, parameter search. Embarrassingly parallel.
- **AI nodes** (GPU): 4 × (2 L40S / 4 RTX 6000 Ada / H100) — embeddings, rerankers, OCR, local models. Claude Code itself needs no GPU.
- **Storage**: start at ~100 TB object storage. Experiment output grows faster than intuition suggests.

---

## 4. Execution Model

### 4.1 Event-driven, not timer-driven

Agents do not poll in a `while True` loop burning compute. **A database write is the trigger** — state transitions emit events; the scheduler converts events into jobs.

```
Strategy spec saved        → enqueue IMPLEMENT
Code passed static checks  → enqueue EVALUATE
Code failed static checks  → enqueue FIX_CODE (bounded retries, then quarantine)
Evaluation CLEARS the bar  → enqueue PROMOTE directly, skipping A3 entirely (§7.5)
Evaluation FAILS the bar   → enqueue REVIEW (A3 only ever sees below-bar attempts)
A3 says "iterate"          → enqueue IMPLEMENT (n+1)
A3 says "plateau"/"reject" → enqueue ARCHIVE (A5) — never PROMOTE
Promotion decided          → enqueue ARCHIVE + notify dashboard
Human approves a gate      → create deployment + merge to deploy branch (§5.3)
Paper trading milestone    → enqueue MONITOR
Health check trips Red     → enqueue lifecycle action + notify dashboard
New document ingested      → enqueue EXTRACT (the Librarian)
High-novelty extraction    → enqueue GENERATE_SPEC directly — skip the nightly wait
Failure pattern detected   → enqueue a research question to the curiosity queue
```

The system is effectively 24/7 but consumes compute only when there is work.

### 4.2 Time-driven jobs — the exceptions

| Cadence | Job |
|---|---|
| Hourly | Poll paper / GitHub / blog collectors |
| Daily, post-close per market | Ingest market data; recompute regime, volatility, correlation stats |
| Nightly | A1 hypothesis batch; daily research report |
| Weekly | A5 cross-experiment pattern mining; knowledge-graph consolidation |
| Weekend | Large parameter sweeps, deep literature review |

### 4.3 Job lifecycle & failure handling

`PENDING → CLAIMED → RUNNING → {SUCCEEDED | FAILED | TIMED_OUT}`

- Claims are atomic, with a **lease and heartbeat**. A dead worker's lease expires and the job returns to `PENDING`.
- Retries are bounded with exponential backoff. **Transient** failures (API timeout, rate limit) retry; **deterministic** ones (code won't compile) do not — they route to A2 as a fix-job with the error attached.
- Every job records inputs hash, outputs, duration, token/compute cost, and error trace.
- **Poison-pill protection:** a strategy whose jobs fail *k* times consecutively is quarantined for human inspection rather than looping forever.

### 4.4 Budgets and back-pressure

Hard caps enforced by the scheduler, all configurable:

- Max concurrent Claude sessions
- Max tokens per day (global) and per strategy (lifetime)
- Max iterations per strategy — backstop to the stop rule
- Max wall-clock per evaluation phase (§9.6)
- Max experiments per day — tied to measured FDR by the autonomy ratchet (§14.4)

When a cap is hit the scheduler stops dispatching that class of work and logs the reason. **Budget exhaustion is a normal state, not an error.**

### 4.5 Continuous operation — and the one legitimate reason to idle ★

**The laboratory runs 24/7 and never stops on success.** Clearing the bar stops *that strategy's* iteration (§7.5); it does not stop the campaign, the goal, or the lab. Research continues while candidates sit in the human queue, while strategies paper-trade, and while others run live. Multiple strategies occupy paper trading simultaneously; the pipeline is continuous, not a one-shot search.

**No agent should sit idle because nothing was queued.** That is a scheduling bug — it means A1 is not generating, or the queue drained, and throughput is being wasted.

**Agents may idle because a safety limit was reached.** That is the system working:

| Idle cause | Verdict |
|---|---|
| Queue empty, budget available, no limit hit | 🐛 **Bug** — investigate, the lab is wasting capacity |
| Daily token/compute budget exhausted | ✅ **By design** (§4.4) |
| Null-world FDR above threshold → autonomy ratchet throttling | ✅ **By design** (§14.4) |
| A family's vault budget exhausted | ✅ **By design** (§14.2) |
| Concurrency cap reached | ✅ **By design** |

The distinction matters operationally: the dashboard must show *which* of these is occurring, so "the lab is quiet" is never ambiguous between "healthy and throttled" and "broken and stalled."

---

## 5. Storage & Git Convention

### 5.1 SQLite *and* git — not either/or

The reference uses git as the entire experiment database. We do not, for one decisive reason: **deflated Sharpe requires a trial count**, and *"how many attempts have been made in this family?"* cannot be answered by grepping `git log`. Paper trading, health monitoring and deployment state reinforce the same conclusion.

His loop is one file evolving in a straight line; ours produces many independent candidates forming no single lineage, so the keep/reset ratchet does not map cleanly.

| Store | Holds |
|---|---|
| **git** | The actual strategy code, diffs, history |
| **SQLite** | Metadata, metrics, provenance, state, trial counts |
| **`experiments.code_commit`** | The hash linking the two |

Strategy code is **never** stored as a blob in the database — that loses diffs, blame, and the ability to check out and re-run a past experiment.

### 5.2 One repo, one branch per strategy ★

**A separate repo per hypothesis was considered and rejected.** Repos are a large, durable unit; creating one per idea — of which there will eventually be millions — means real overhead per hypothesis, no way to search across all of them at once, and an unbounded backup problem.

**Branches are the right size.** A branch is a cheap pointer to a commit chain — exactly the shape of one hypothesis's story:

```
ONE repo, forever
├── branch: strategy/<strategy_id>      one per STRATEGY
│      commit 1: iteration 1              (not per experiment, not per family)
│      commit 2: iteration 2              each iteration is another commit
│      commit 3: ...                       on the same branch
├── branch: strategy/<other_id>
├── branch: deploy/paper                 §5.3
└── branch: deploy/live                  §5.3
```

This is the reference project's "one branch per campaign" convention, scaled from one hypothesis to many running concurrently.

**Why a branch is technically mandatory, not merely tidy:** git's garbage collector only protects commits reachable from a branch or tag. `experiments.code_commit` records a hash in SQLite, but **git has no knowledge of that database** — a commit with no branch pointing at it is unreferenced, and `git gc` can eventually delete it. A live branch per strategy is what guarantees a `code_commit` pointer never silently breaks.

**Branches are never deleted**, including for rejected strategies. The branch *is* the permanent research record, and it is cheap to keep forever.

**File-layout consequence:** nanoAQRL's single `strategy.py` (§2.1) works only because exactly one hypothesis is live at a time. Once strategies coexist, each must live at its own path — `strategies/<strategy_id>/strategy.py` — so two strategies' code can be merged into one branch without touching the same file.

### 5.3 Merging — only forward, only on approval

**Most branches are never merged anywhere.** Two unrelated hypotheses (an SMA crossover and a mean-reversion RSI strategy) have no shared content to combine. A rejected or plateaued strategy's branch simply stays where it is, permanently.

**Merging happens at exactly one moment: when a human approves a strategy to run.**

```
Human approves research → paper   →  merge strategy/<id> into deploy/paper
Human approves paper → live       →  merge strategy/<id> into deploy/live
```

This gives both human gates a concrete, physical, auditable action instead of only a database row. Because each strategy lives at its own path (§5.2), merging many strategies into one deploy branch is conflict-free by construction.

**The merge commit doubles as an audit record.** Its message references the `promotions` row that authorised it — approver, timestamp, promotion ID — so `git show deploy/live` answers *"what is trading right now"* unambiguously, and the git history and database cross-reference each other.

**Retirement removes a strategy from its deploy branch, never from its research branch.** `deploy/live` reflects only what is *currently* running; `strategy/<id>` keeps the full history forever regardless.

---

## 6. The Evaluation Engine — `evaluate.py`

**One evaluation engine. Not one per market. Not one per timeframe.**

### 6.1 Why forking the engine is forbidden

The statistical layer — deflated Sharpe, White's Reality Check, CSCV/PBO, Monte Carlo, walk-forward — is market-agnostic mathematics and must exist exactly once.

The reason is not code hygiene, it is **scientific validity.** The value of the Research Memory depends on A5 being able to compare experiment #6,201 in crypto against #12,483 in Indian equities. That comparison is only meaningful if both were scored by identical code. Fork `evaluate.py` six ways and within a year you have six subtly divergent PBO implementations, a Sharpe of 1.4 no longer means the same thing in two rows of the same table, and every cross-market lesson the knowledge graph produces is noise — **silently.**

### 6.2 The factoring

```
evaluate.py                 # one engine: phases, statistics, the hard bar
  ├── MarketProfile         # calendar, costs, constraints, benchmark, risk-free, currency
  ├── TimeframeProfile      # annualisation, fill model, cost sweep, WF windows, min-N
  ├── validators/           # per-market data sanity checks
  └── gates/                # optional extra phases per market (§6.5)
```

Two composable axes. Six markets × five timeframes = **30 profile files, not 30 engines.**

### 6.3 MarketProfile — what genuinely differs by market

Inputs, never different algorithms:

| Group | Contents |
|---|---|
| **Calendar** | Session hours, holidays, half-days. NSE 09:15–15:30; MCX to 23:30; crypto 24/7; forex 24/5 with a Sunday open |
| **Costs** | Resolved **per market *and* per asset class** (§6.3a) — the same venue charges a cash equity, an ETF and a derivative differently |
| **Constraints** | Tick size, lot size, contract multiplier, margin, circuit limits (India) vs halts (US), short-selling rules (Indian cash equities: intraday only) |
| **Data hazards** | Survivorship and delisting (equities), contract roll/contango (commodities), exchange-specific bad prints (crypto) |
| **Reference** | Risk-free rate source, benchmark for alpha/beta, P&L currency, ADV/liquidity cap |

### 6.3a Cost models are per market × asset class ★

Cost is **not** a market-level property. NSE charges a delivery equity trade, an intraday equity trade and an index future entirely differently. The resolved cost model is therefore keyed on `(market, asset_class)`, and `asset_class` is a first-class field on every instrument.

**Asset classes in scope:** cash equity · index ETF · index/commodity **future** · **CFD** · **spot crypto** · **crypto perpetual**.

#### Reference: NSE cash equity, delivery — derived 2026-07

| Component | Rate | Round-trip |
|---|---|---|
| STT | 0.1% buy **and** sell | **20.0 bps** |
| Stamp duty | 0.015% buy only | 1.5 bps |
| Exchange transaction | 0.00345% each side | 0.69 bps |
| SEBI turnover | 0.0001% each side | 0.02 bps |
| Brokerage | ₹0 delivery (discount broker) | 0 bps |
| GST | 18% on (brokerage + exchange + SEBI) | 0.13 bps |
| **Statutory total** | | **≈ 22.3 bps** |
| Slippage (NIFTY-50 liquidity) | 2–5 bps per side | 4–10 bps |
| **Realistic all-in** | | **≈ 27–32 bps** |

> **This single number shapes what is even worth researching.** A swing strategy must average **> 30 bps per round trip** merely to break even, and the 2× cost stress means clearing **~55–65 bps**. At 100 trades/year that is roughly 3% of turnover consumed annually before any edge exists.

**Provenance requirement:** these figures were derived from public sources and are a *starting default only*. Before any live capital, each market's cost model must be **re-derived from a real broker contract note** and the source recorded on the profile. Published rates go stale, differ by segment, and change with budgets — a cost model sourced from a blog is a silent, systematic bias in every backtest that uses it.

### 6.4 TimeframeProfile — what genuinely differs by timeframe

Different in kind, and the primary source of silent self-deception:

**Supported range: 1 second to 1 month.** The architecture must span roughly seven orders of magnitude in bar size, which makes every field below profile-driven rather than assumed.

| Field | Why it matters |
|---|---|
| **`periods_per_year`** | √252 daily vs √(252×375) for 1-minute vs ~5.7M periods/year for 1-second NSE. **Must come from the profile — never a hardcoded constant.** One wrong value makes every Sharpe in the database fiction |
| **`fill_model`** | Monthly/daily: next-open. Intraday: bar-level rules + spread-fraction slippage. **Sub-minute: queue position and latency assumptions we do not yet trust** — see the warning below |
| **`cost_stress_multipliers`** | At 1-second, costs and spread dominate entirely; at monthly they are a rounding error |
| **Walk-forward windows** | Sized in **bars** for statistical power *and* **calendar time** for regime coverage. At 1-second a 1-year test window is ~5.7M bars; at monthly it is 12 |
| **`min_trades`** | Ties directly to the promotion rule (PRD §9.3) |
| **Overnight handling** | Gap risk, carry, crypto funding accrual — only if positions cross sessions |

> ⚠️ **Honesty limit at the fast end.** Below roughly 1 minute, backtest realism degrades sharply: fills depend on queue position, latency and order-book depth that bar data cannot represent. The architecture *supports* 1-second bars; that is not the same as the results being trustworthy there. Sub-minute strategies should carry a much heavier slippage assumption and be treated as research artifacts until validated by real paper-trading fills. This is a data-fidelity limit, not a code limit.
>
> **Storage note:** 1-second bars for 50 instruments across 25 years is on the order of terabytes. Fast timeframes should be scoped to shorter histories or fewer instruments rather than assuming full coverage.

Illustrative:

```yaml
market: nse_equity
  calendar: nse
  costs:       {brokerage_bps: 3, stt_bps: 10, stamp_bps: 1.5, gst_pct: 18}
  constraints: {tick: 0.05, short_intraday_only: true, circuit_pct: 20}
  benchmark:   NIFTY50
  risk_free:   india_tbill_91d
  currency:    INR

timeframe: daily
  periods_per_year: 252
  fill_model: next_bar_open
  cost_stress_multipliers: [1, 2, 3, 5]
  min_trades: 100
  overnight: true
```

### 6.5 Legitimate per-market variation — extra gates, not extra engines

Additional phases **appended** to the standard sequence, so core metrics stay identical and comparable:

- **Crypto:** venue robustness (does the edge survive on a second exchange?), funding-cost sensitivity
- **Equities:** survivorship-bias check, capacity/ADV constraint
- **Commodities:** roll-method sensitivity (does the edge depend on how contracts were stitched?)
- **Forex:** session-of-day dependence, carry decomposition

### 6.6 Provenance — mandatory on every experiment ★

```
eval_engine_version      # semver of evaluate.py
market_profile_hash      # content hash of the resolved profile
timeframe_profile_hash   # content hash of the resolved profile
wf_config_hash           # hash of {scheme, train_years, test_years} — §8.2
code_commit              # git commit of the strategy code
data_snapshot_id         # exact dataset version
operator_library_version
random_seed
```

**Why:** the day cost assumptions change — and they will — we must instantly answer *"which of my 40,000 stored results are still comparable?"* Without this, the knowledge base silently mixes results scored under different rules. That is precisely the rot that kills long-running research systems.

Changing any profile or the engine bumps a version. Old results are never deleted; they are marked `comparable = 0` and optionally queued for re-evaluation.

### 6.7 The invariant

The reference achieves comparability with a single constant — the 5-minute wall clock — rather than a versioning scheme. The AQRL analogue is a **fixed evaluation contract**: data slice, cost model, walk-forward configuration and test protocol held constant across a campaign. §6.6 enforces it; the design goal is to keep the contract simple enough that it rarely changes, because **every change partitions the result history.**

---

## 7. The Honest Score ★

> ```
> score = SR_oos  −  2 × SE(SR)  −  SR*(N_trials)
> ```
>
> **The deflated lower bound on out-of-sample Sharpe.** In words: *what Sharpe can we be confident is real, after accounting for how few trades we have, how ugly the tails are, and how many things we already tried?*
>
> `evaluate.py` returns **one float**. That float alone drives keep/discard.

### 7.1 Why one number, and why this one

Running every test in `evaluate.py` does not produce a score. Thirty metrics is a **report**; it cannot answer *"is experiment 47 better than 46?"* The score is a decision rule, chosen deliberately.

The reference project works because it has one honest scalar. `val_bpb` is held out, vocab-independent (so architectures compare fairly), a single number, and effectively impossible to game. Our equivalent must satisfy:

| `val_bpb` property | AQRL requirement |
|---|---|
| Held out | Computed only on data the search never touched |
| Vocab-independent | **Frequency-fair** — 5-trades/year and 50-trades/day must be comparable |
| Single scalar | One number, so "better or worse" is unambiguous |
| Hard to game | **Not raw Sharpe** — inflatable via leverage and frequency effects |
| — | Cost-inclusive by construction, not adjusted afterwards |

### 7.2 The three terms

**Term 1 — `SR_oos`: Sharpe on data the search never fitted.**
Generated by purged rolling walk-forward with an embargo gap (§8). All test windows concatenate into one return series. **That series is the only thing ever scored** — never the fitted sample. Computed at **2× assumed costs**; cost stress is the default condition, not a separate later test.

**Term 2 — the uncertainty haircut.** The standard asymptotic standard error of a Sharpe estimate:

```
SE(SR) = sqrt( (1 + SR²/2 − skew·SR + (kurtosis−3)/4 · SR²) / n )
```

This closes three attack vectors with one formula, without a rule for each:

| Gaming attempt | Why it fails automatically |
|---|---|
| Trade almost never — 4 trades, all winners | `n` small → error bar huge → lower bound collapses |
| Pick up pennies in front of a steamroller | negative skew → `−skew·SR` positive → SE grows → score drops |
| Rare catastrophic tail | kurtosis term grows → score drops |

**Term 3 — the trials haircut, `SR*(N_trials)`.** Try N strategies on pure noise and the best will show a respectable Sharpe by luck alone. That expected-best-under-null is computable from N and subtracted (the deflated Sharpe construction, Bailey & López de Prado). Try 10 things, subtract a little; try 10,000, subtract a lot. This is why `strategies.family` and the trial count exist in the schema.

**What counts as a trial is defined precisely in §8.6** — and it includes the ×3 from evaluating every strategy at three train-window lengths and reporting the best (§8.2). Under-counting here is the single easiest way to make this whole score dishonest.

### 7.3 Why a bound and not a probability

The Probabilistic Sharpe Ratio returns a probability, which **saturates** — two good strategies both score 0.99 and the hill-climb loses its gradient. The loop needs a number that keeps moving so the agent can tell it is making progress, exactly as `val_bpb` does. A lower confidence bound provides that; a probability does not.

### 7.4 Rejected alternatives

| Alternative | Why not |
|---|---|
| Raw Sharpe | Ignores selection, tails and sample size — this is the trap |
| Total return / CAGR | Leverage-gameable; ratios are not |
| Calmar (return / maxDD) | Max drawdown is one noisy worst-moment statistic. Fine as a gate, bad as a ranking |
| Probabilistic Sharpe | Saturates — §7.3 |
| A weighted blend of ten metrics | Every weight is a knob that gets tuned until the answer looks good. That is overfitting your own scorer |

### 7.5 The bar and the score are separate ★

A hard pass/fail bar runs **before** any score is computed.

| The bar (pass/fail, pre-registered) | Value | The score (ranking) |
|---|---|---|
| Minimum honest score | **0.50** | `SR_oos − 1.65·SE(SR) − SR*(N)` |
| **Maximum out-of-sample drawdown** | **15%** — **20% for crypto** | |
| Minimum trade count | **100** | |
| Minimum breadth across instruments | *TBD* | |
| Profitable at 2× costs | required | |
| Maximum complexity | *TBD* | |
| **`z` multiplier on the haircut** | **1.65** (~95% one-sided) | |

Fail any item → **`discard`, no score computed, stop.**

> Breadth and complexity are deliberately still open (§20) — both need a measurement definition before they can carry a number.

**Drawdown deliberately does not enter the score.** Max drawdown is a single worst-moment statistic — very noisy, highly dependent on the sample window. Ranking on it means ranking partly on luck. As a *gate* its noisiness is harmless; as a *ranking* it is corrosive.

**Clearing the bar is an immediate, unconditional stop.** The first passing iteration is the last one — the worker routes straight to A4 without invoking A3 (PRD §9.2, App-Flow §6.1). Since the bar already contains a minimum score, clearing it already means "good enough by a standard set in advance."

**Gates are enforced in `evaluate.py`, not merely stated in `program.md`.** `program.md` is *instructions* — the agent decides whether it complied, and will eventually persuade itself that 40 trades is close enough to 100. The bar therefore lives in both files with different jobs: `program.md` states the target; `evaluate.py` **enforces** it. Since the agent can neither read nor edit `evaluate.py`, the gate is a fact rather than a request.

### 7.6 Ranked criteria

1. **Primary** — the honest score
2. **Secondary** — a resource constraint: capacity or turnover (the analogue of the reference's VRAM ceiling)
3. **Tertiary** — **simplicity**, scored rather than left to reviewer judgment

### 7.7 Frequency fairness

Annualising by `√periods_per_year` is fair **when trades are independent** — a higher-frequency strategy genuinely gets more independent observations, so a higher Sharpe is real rather than an artifact. The unfairness appears with **autocorrelated returns** (overlapping positions, slow-decaying signals), which require an autocorrelation correction or the Sharpe is inflated.

Capacity — which genuinely favours lower frequency — stays as the **secondary ranked criterion**, never blended into the primary score, so neither can hide the other.

### 7.8 Out-of-sample data is a consumable resource ★

Every iteration against the walk-forward window makes it slightly less out-of-sample. After a few thousand iterations it is effectively in-sample — it has simply been fitted more slowly.

The trials haircut compensates mathematically and the vault (§14.2) protects the final promotion decision, but the real defence is **satisficing** (PRD §10.2). The loop stops at the first strategy clearing the bar not out of modesty, but because **every extra iteration spends a resource that cannot be refilled.**

---

## 8. The Walk-Forward Protocol

### 8.1 Scheme and windows — decided

| Scheme | Shape | Notes |
|---|---|---|
| **Rolling / sliding** ✔ | train 2016 → test 2017; train 2017 → test 2018 … | Fixed train length, slides forward. **This is what we use** |
| Anchored / expanding | train 2015–2020 → test 2021; train 2015–2021 → test 2022 … | Fixed start, growing train set |
| Single holdout | train 2000–2015 → test 2016–2025 | One fold. Simple, high variance |
| Combinatorial purged CV | many valid train/test combinations across N groups | Yields a *distribution* of outcomes. What PBO is built on. Expensive — deferred past Stage 0 |

**Decision: rolling window, purged, embargo ≥ holding period.**

- **Test window: always 1 year.** Fixed, non-negotiable. It represents the strategy's realistic re-fit and re-validation cadence in production, which is independent of how much history each fit sees.
- **Train window: every strategy is evaluated at all three lengths — 1, 2 and 3 years.** The reported score is the **best** of the three (§8.2).

Rolling over anchored because train and test lengths stay constant, so **folds are comparable to each other.** Anchored fails that — its later folds carry several times the training data of its early ones. Anchored is better only when data is scarce, which at 25 years it is not.

**What the train window trades off:**

- **Fit stability vs recency.** More train years gives more data to estimate parameters robustly — relevant if the strategy relies on slow-moving structure. But a longer window anchors each fold's fit to older information relative to the 1-year test that follows, so the strategy adapts more slowly if the relationship drifts.
- **Effect on `n` is real but modest.** Because the test window is always 1 year, total OOS observations scale as `(dataset_years − train_years)`. On ~26 years: train=1yr → ~25 years of OOS; train=3yr → ~23. An ~8% difference — the stability-vs-recency tradeoff dominates, not the width of `SE(SR)`.

Evaluating all three means **3× the compute per experiment.** That is a real throughput cost, budgeted for in §9.

### 8.2 Best-of-three, and the trial count that makes it honest ★

**Decision: run all three train windows, report the best.**

```
honest_score = max( score(train=1yr), score(train=2yr), score(train=3yr) )
```

**Taking the best of three configurations is a selection**, and selections inflate scores — with enough configurations, noise alone produces a good-looking winner. This is the same hazard as trying many strategies and keeping the best.

**The deflated Sharpe already handles exactly this, provided it is told the truth about how many things were tried.** So the rule is not "don't select" — it is **"select freely, but count every configuration in `N_trials`."**

```
N_trials for a family  =  Σ over iterations of (train windows evaluated)
                       =  iterations × 3
                       + prior related experiments in the same family
```

Consequences, stated plainly:

- The trials haircut is **three times larger** than it would be under a single fixed window. That is the honest price of taking the best, and it makes the bar harder to clear.
- All three scores are stored (`score_train_1y`, `score_train_2y`, `score_train_3y`) alongside which one won. A strategy that scores 0.61 / 0.58 / 0.60 is robust to history length; one that scores 0.62 / 0.11 / 0.09 is not — **and that spread is a first-class diagnostic even though it does not gate.**
- The winning window is recorded on the experiment, so "which history length does this edge need?" becomes a queryable, aggregatable question across the whole archive.

**The scheme itself remains fixed.** Rolling vs anchored vs CPCV is *not* searched over — running rolling, then anchored, and reporting whichever scored better would be a selection **outside** the trial count, invisible to the haircut. One scheme per campaign, fixed in `evaluate.py` before searching begins, hashed into `wf_config_hash` (§6.6).

**An emergent property worth naming:** because `evaluate.py` is off-limits to the agent, the walk-forward configuration — scheme, the set of train windows, test length — **cannot be an iteration lever.** A3 may propose changes to `strategy.py`, but it can never propose "try a different training window," because that is part of the evaluation contract, structurally outside its reach. The best-of-three happens inside `evaluate.py` on every experiment identically, so it cannot be gamed by choosing when to apply it.

### 8.3 Combining folds — concatenate, always

**The score is computed from a single concatenated series.** All test-window returns are joined end to end into one out-of-sample track record; `SR`, `skew`, `kurtosis` and `n` are computed once over that series.

The alternative — scoring each fold and averaging — is **not** used for the score.

**Per-fold metrics are still computed and stored** (`fold_metrics`, `folds_profitable`, `wf_efficiency`) because they diagnose something concatenation hides: a strategy brilliant in 3 folds and terrible in 5 can still concatenate to a respectable Sharpe. They inform promotion review and A3's reasoning; they do not drive keep/discard.

**Walk-forward efficiency** (out-of-sample ÷ in-sample performance) is the key diagnostic — if OOS is far below IS, each fold is overfitting internally even though the concatenated series looks acceptable.

### 8.4 Purging and embargo

Applied to whichever scheme is in use. **The embargo gap must be ≥ the strategy's holding period**, or trades straddle the train/test boundary and leak. This is a hard requirement, verified by test.

### 8.5 What walk-forward actually tests — and the tuning rules ★

Walk-forward validates the **fitting process**, not the strategy. Each fold re-runs parameter selection on the training window alone and checks whether the result survives the next period.

**Decision: the agent tunes parameters.** Therefore:

- **Every fold re-runs the tuning from scratch, on that fold's training window only.** If tuning ever touches the test window, the entire exercise is theatre and the OOS number is fiction.
- The tuned parameters chosen in each fold are recorded, so parameter drift across folds is inspectable — a strategy whose optimal parameters swing wildly between folds is unstable regardless of its score.

### 8.6 Does parameter tuning inflate the trial count? — a subtle but decisive distinction ★

**No, provided the tuning selects on training data only.** This is worth getting exactly right, because getting it wrong makes the haircut either useless or impossible to clear.

The deflated Sharpe's `N` counts **selections made on the metric being reported.** So:

| Activity | Selects on | Counts toward `N_trials`? |
|---|---|---|
| Parameter tuning **inside a fold**, on training data | Train performance | ❌ **No.** It never saw the test window. It is part of the *procedure being evaluated*, not a selection over reported outcomes |
| Choosing the **best of 3 train windows** by OOS score | The reported OOS score | ✅ **Yes — ×3** (§8.2) |
| Each **A2↔A3 iteration**, where a change is made after seeing the OOS result | The reported OOS score | ✅ **Yes — +1 each** |
| Prior experiments in the **same family**, including the same idea in another market | The reported OOS score | ✅ **Yes** |

The principle: *if a human or agent looked at an out-of-sample number and then changed something, that is a trial.* If a procedure fitted itself on training data with no view of the test set, that is just the procedure.

**But tuning is not free — it moves the risk somewhere else.** A large parameter grid does not inflate the haircut; it inflates the chance that each fold overfits *internally*, which shows up as **walk-forward efficiency** (§8.3) collapsing — strong in-sample, weak out-of-sample, fold after fold. That is the diagnostic to watch, not `N_trials`.

**Recommended starting grid: ≤ 50 combinations per fold**, coarse rather than fine. Rationale: the compute cost is already 3× from the train-window sweep (§8.2), a coarse grid is far less prone to fitting fold-specific noise, and if `wf_efficiency` stays healthy the grid can be widened later with evidence. Widen only in response to a measured need, never by default.

---

## 9. Performance & Parallelism

**Speed is a first-class requirement, because throughput is what makes the loop viable at all.**

### 9.1 Why it decides whether the lab works

| `evaluate.py` runtime | Experiments overnight (12h, 1 core) |
|---|---|
| 1 second | ~43,000 |
| 10 seconds | ~4,300 |
| 2 minutes | ~360 |
| 20 minutes | ~36 |

That is the difference between a research laboratory and a slow notebook. **Target: `evaluate.py` completes in seconds, not minutes.**

### 9.2 Vectorise the maths, JIT the path

- **Vectorised NumPy / Polars** for everything expressible as array maths. No Python loops over bars.
- **Numba `@njit` for genuinely path-dependent logic** — trailing stops, position state, sequential fills. These cannot be vectorised honestly, and a JIT loop is both far faster than Python *and* far easier to keep correct than a contorted vectorised version.
- **Polars over pandas** for large frames; **DuckDB** for analytical queries straight over Parquet.
- **Memory-mapped columnar reads.** Load only the columns and date range a fold needs.
- **Compute indicators once per snapshot, not once per fold.** Across ~24 rolling folds this is the single largest easy win.

### 9.3 Parallelism — processes, not threads ★

**Python threads do not speed up CPU-bound backtesting.** The GIL serialises them; you get complexity and no throughput.

- Use **`multiprocessing` / `joblib`** across independent units of work.
- **NumPy and Numba release the GIL** internally, so vectorised work already uses hardware efficiently within one process.
- Free-threaded CPython builds are maturing but should not be depended on.

Embarrassingly parallel, in priority order:

| Work | Parallel across | Notes |
|---|---|---|
| Walk-forward folds | ~24 folds | Fully independent — the biggest single win |
| Monte Carlo / bootstrap | replications | Trivially parallel |
| **Null-world calibration** | replications × null models | The heaviest job in the system; parallelise hard |
| Parameter sweeps | combinations | |
| Multiple experiments | strategies | Later stages, once the queue exists |

Parallelise at the **outermost independent level** — the inner loop should already be vectorised or JIT-compiled.

### 9.4 Vectorisation is the top source of look-ahead ★

**This is the one place where "make it fast" fights "make it honest," and speed must not win.**

```python
df['signal'] = df['close'] > df['ma']                  # signal from THIS bar's close
df['ret'] = df['signal'] * df['close'].pct_change()    # ...traded at THIS bar's close
```

Others: rolling z-scores or percentile ranks over the **whole** series; `fillna(method='bfill')` pulling values backwards; centred windows; normalisation fitted on the full sample before splitting.

Consequences:

- **P0's look-ahead checks (§10.1) matter more as the code gets more vectorised**, not less.
- Every signal must be explicitly lagged, and that lag **verified by test**, not by eyeballing.
- Known-answer tests must include a **deliberately leaky vectorised strategy** that P0 is required to catch.
- The prohibitions are written into `program.md` (§2.4) so the agent avoids them by default — but that is error reduction, not enforcement.

### 9.5 Determinism under parallelism — non-negotiable

Parallel execution must not change results. A score that shifts between runs destroys the comparability everything else rests on.

- **Seeds derived per fold / per replication** from a base seed — never from wall clock or worker ID.
- **Deterministic reduction order** — floating-point summation is not associative, so results must combine in a fixed order regardless of which worker finishes first.
- Fold returns concatenate in **chronological order**, never completion order.
- The same experiment re-run must produce a **bit-identical** `honest_score`.

### 9.6 Per-experiment time budget

Borrowed from the reference's fixed wall clock: an experiment exceeding its budget is **killed and recorded as `crash`**. This keeps overnight throughput predictable and stops one pathological strategy consuming a whole night.

### 9.7 Order of work

**Correct first, then measure, then optimise the measured bottleneck.** A fast wrong answer is worse than a slow one, because it is wrong at scale. But nothing in the architecture may *preclude* speed — hence vectorised structures, process-level parallelism and columnar storage from the start.

Profile before optimising. On this workload the bottleneck is usually data loading and per-fold indicator recomputation, not the maths.

---

## 10. The Validation Battery

An ordered funnel. Cheap tests first; a failure short-circuits the rest.

| Phase | Name | Contents | Cost |
|---|---|---|---|
| **P0** | Smoke & correctness | Compiles, runs, produces trades. **Look-ahead & leakage static checks.** No NaN/inf | Seconds |
| **P1** | Fast backtest | Small slice, frictionless. Is there any signal at all? | Seconds–minutes |
| **P2** | Full backtest | Complete history, realistic costs and fills, correct calendar | Minutes |
| **P3** | Robustness battery | Walk-forward, Monte Carlo, deflated Sharpe, White's Reality Check, CSCV/PBO, regime analysis, cost sensitivity, parameter sensitivity | Minutes–hours |
| **P4** | Paper trading | Forward evidence in live market conditions | Weeks–months |

### 10.1 P0 is the most underrated

Look-ahead bias and data leakage are the dominant failure modes of LLM-written strategy code. P0 must include automated checks:

- No use of future bars in signal computation (shift/lag verification)
- No fitting on the full sample before splitting
- No survivorship-biased universe construction
- No use of point-in-time-unavailable fundamentals
- Signal → order → fill ordering respects the fill model

**A strategy that fails P0 is a bug**, routed back to A2 with the diagnostic. It is not a research finding and must never pollute the knowledge base as one.

### 10.2 Multiple-testing discipline

The deflated Sharpe requires the **number of trials**, which must include:

- Every iteration of the A2↔A3 loop for this strategy
- Every parameter combination swept
- The related experiments already run in the same **family**

Under-counting trials makes deflated Sharpe a rubber stamp. The database must answer *"how many trials in this family?"* as an indexed query — a hard schema requirement, not an afterthought.

**Trying the same idea across 3 markets is 3 trials, not 1.** The family grouping exists precisely so this cannot be miscounted.

### 10.3 Gate outcomes

Each phase yields `PASS | FAIL | WARN` per test plus a numeric value and its threshold. Thresholds are configuration, versioned alongside profiles.

---

## 11. The Operator Library

**The LLM does not invent arbitrary formulas.** It composes from a vetted, versioned library. This shrinks the search space enormously, makes strategies interpretable and reviewable, and makes results comparable across experiments.

| Category | Examples |
|---|---|
| **Transformations** | Rolling mean, EMA, JMA, Kalman filter, ATR normalisation, wavelets, PCA, fractional differencing |
| **Signal** | Crossovers, thresholds, breakouts, volatility expansion, momentum, mean reversion, volume confirmation |
| **Risk** | ATR stop, time stop, trailing stop, position sizing, Kelly variants, volatility targeting |
| **Portfolio** | Risk parity, equal weight, correlation clustering |

### 11.1 Requirements

- Every operator is **versioned, unit-tested**, and documented with valid parameter ranges.
- Every operator declares which timeframes and markets it is valid for.
- A strategy spec is a **composition of operators expressible as a DAG** — making specs diffable, hashable and searchable for near-duplicates.
- **Duplicate detection:** the spec's canonical hash is checked against all prior specs before any compute is spent. An identical composition is rejected at insert; near-duplicates surface the prior result to A3.
- New operators may be proposed (by A1 from literature, or by a human) but enter the library only via **explicit review with tests**. The library is not self-modifying.

---

## 12. Knowledge Subsystem

### 12.1 Internal memory — two layers

Append-only. Key requirement: **every experiment must be reproducible from its stored record alone** — spec, code version, data snapshot, profiles, seeds.

| Layer | Written by | When | Cost |
|---|---|---|---|
| **Raw record** — every experiment with full evaluation results | Automatically | Every experiment, immediately | Free (a DB write) |
| **Synthesized lesson** — lab notebooks, knowledge entries, graph edges | A5 | Once, when a strategy's story concludes | An LLM call |

A5 reads the **complete** set of a strategy's iterations at once and writes one well-formed lesson, rather than a half-formed summary after each attempt. Nothing is lost — the raw layer already captured everything — and the synthesis is both better-informed and cheaper for being done once.

### 12.2 External ingestion — the Librarian ★

A **single uniform pipeline for every source type.** A GitHub source contributes its text (README, docs, comments) through the same path as a paper or a blog post — no separate code-graph or AST tooling.

```
Collectors (Python, scheduled — arXiv, SSRN, GitHub, blogs, market data)
      ▼
Dedup by content_hash · cheap relevance filter (before any LLM cost)
      ▼
┌──────────────── THE LIBRARIAN ────────────────┐
│  Big document? → chunk by STRUCTURE            │
│  (sections/headings — never a blind token      │
│   window, so a formula is never split)         │
│           ▼                                    │
│  PASS 1 — per chunk: what claim/method is here?│
│           ▼                                    │
│  PASS 2 — synthesize ACROSS chunks into a few  │
│  DISTINCT ideas. A 40-page paper usually        │
│  yields 2–3, never one blob                    │
│           ▼                                    │
│  Classify + tag evidence_tier = external_claim │
└───────────────────┬────────────────────────────┘
                    ▼
   One external_knowledge row PER IDEA,
   each carrying source_chunk_ids back to the exact passage
                    ▼
              Consumed by A1
```

**Claude is not a crawler.** Collectors fetch; the Librarian's extraction pass converts each document to structured records; the raw document is archived and **never re-read**. Every later access — by A1, by a human, by anything — is to the structured rows.

**Chunking and synthesis policy:**
- **Chunk by meaning, not fixed size.** A blind 2,000-token window risks cutting a method description or equation across a boundary.
- **Two passes, not one.** Skipping pass 2 is the most common mistake — it produces one giant undifferentiated summary per document instead of individually testable ideas.
- **Traceability.** Every synthesized idea records which chunks it came from, so *"why do we believe this"* resolves to an exact passage.
- **Read once, ever.** Chunking means "read once, in pieces, ever."

### 12.3 Trust tier — a claim is not a fact ★

Everything the Librarian writes is tagged `evidence_tier = external_claim`. Its `extraction_confidence` measures **the Librarian's confidence that it read and summarised the source correctly** — explicitly *not* a claim that the underlying idea is true.

That distinction is easy to blur and costly to blur: a plausible-sounding paper is not evidence; an executed and validated experiment is. Only `knowledge_entries` (internal) carry tested-evidence weight. **A strategy is never promoted on the strength of "a paper said so."**

### 12.4 Curiosity queue

Failure patterns and A3/A5 observations generate **research questions** carrying a topic, motivation and originating experiment. Collectors consult this queue to run targeted searches rather than only broad sweeps. Each question tracks whether it *ever produced a usable hypothesis*, so the loop's own value is auditable.

### 12.5 Knowledge graph

A5 maintains a graph of concepts and conditional relationships:

```
Momentum ──works-in──▶ Trending, Low-Volatility
         ──fails-in──▶ Sideways, High-Volatility
JMA      ──pairs-well-with──▶ ATR
         ──pairs-poorly-with──▶ RSI
         ──effective-in──▶ Commodities
```

Edges carry **evidence counts and confidence** and link back to the experiments supporting them. **An edge with no experiment backing must not exist** — no agent may assert a relationship from pure reasoning.

---

## 13. Data Layer

| Store | Purpose |
|---|---|
| **SQLite** (v1) → **PostgreSQL** (v3) | Experiment metadata, jobs, state machine, knowledge |
| **Parquet** | Market data, tradebooks, equity curves, per-trade records |
| **DuckDB** | Analytical queries over Parquet without loading into Python |
| **Vector index** | Embeddings for literature and prior-experiment similarity |
| **git** | Strategy code (§5) |
| **Object storage** (v3) | Raw documents, large artifacts, archived runs |

### 13.1 Market data requirements

- **Immutable, versioned snapshots.** An experiment references a snapshot ID, never "whatever was on disk that day."
- **Point-in-time correctness** — no restated data leaking backward.
- Per-market validators run **at ingest**, not at experiment time.
- Adjustments (splits/dividends) recorded as a **versioned method**, since the choice affects results.

---

## 14. Adversarial Integrity & Self-Calibration ★

The three mechanisms that make automated search in markets defensible. **None are optional, and all three precede any real-data result.**

### 14.1 Reward hacking is a certainty, not a risk

Give an agent a scoring function and enough iterations and it will optimise the scorer rather than the market — usually by accident, through a subtle look-ahead path, a fill assumption, or a near-zero denominator.

Defences:

- **`evaluate.py` is neither readable nor writable by the agent** (§2.3). Separate process, no source access.
- **`data.py` is read-only.** An agent able to edit the cost model will eventually make costs cheaper and call it a discovery.
- **"Too good to be true" tripwire.** Sharpe > 3 on daily data is a *bug hypothesis*, not a discovery — auto-route to adversarial audit rather than promotion.
- **Periodic red-teaming.** Deliberately task an agent with breaking `evaluate.py`; treat every exploit found as a high-value knowledge entry, and fix it.

### 14.2 The vault — data the loop cannot read

Every other protection — deflated Sharpe, walk-forward, PBO — depends on honestly counting trials. Once an LLM generates hypotheses influenced by memory of past results, the effective trial count becomes **genuinely unknowable.** The vault is the one defence that does not depend on counting anything.

- A span of years, a set of instruments, and/or an entire market is **locked away**.
- The research loop has **no read path**. Not "should not" — *cannot*.
- Opened only at promotion, **once per strategy family**.
- Every open is logged and counts against a lifetime budget.
- A family that exhausts its budget cannot be promoted again until genuinely new data exists.

### 14.3 Null-world calibration — measuring our own false discovery rate

The procedure from PRD §4.2, as an engineering requirement:

- Generate datasets with **no alpha by construction**: permuted returns, block bootstrap, synthetic paths with matched volatility and fat tails.
- Run the **complete loop** — generation, iteration, evaluation, promotion recommendation — against them, through the identical code path as real data. No shortcuts, no special-casing.
- Count reported discoveries. **That count is the false discovery rate.**

Requirements:
- A **permanent regression test** after any change to `evaluate.py`, the scoring rule, or any profile.
- Results recorded in `null_world_runs` and surfaced on the Laboratory screen beside cost-per-discovery.
- Records `max_score_observed` in noise — the bar any real result must clear.
- **Milestone 0.** No real-data result is trusted before FDR has been measured and driven low.

### 14.4 The autonomy ratchet

Experiment throughput is **tied to measured FDR**. If FDR rises, throughput automatically drops. Scaling becomes earned rather than assumed — the reference project's ~100 experiments overnight is safe only once the pipeline has demonstrated it does not invent discoveries at that volume.

---

## 15. LLM Integration Requirements

- **Model:** Claude, via Claude Code sessions. Stateless and disposable.
- **Context assembly is a Python responsibility.** The worker builds the brief — relevant knowledge, prior iterations, evaluation report, operator catalog — and hands Claude a complete packet. Claude does not go hunting for context.
- **Structured outputs.** Every agent returns schema-validated JSON. Free text goes in dedicated reasoning fields, never mixed with machine-read values.
- **Prompt versioning.** Prompts are versioned artifacts in the repo; the version is recorded on every agent output. A prompt change is a system change and affects comparability.
- **Determinism where possible.** Seeds recorded. Where the LLM is inherently non-deterministic, the *output artifact* is stored so the experiment remains reproducible even if regeneration would differ.
- **Cost accounting.** Tokens and dollars recorded per job, per strategy, per discovery.

---

## 16. Observability & Audit

- **Structured logs** for every job, with correlation IDs threading `strategy → experiment → job`.
- **The "why" record.** Every agent decision stores its reasoning and the evidence it cited. A hard requirement (PRD §11.1), not a nice-to-have.
- **Lab notebook per experiment**, auto-generated:
  ```
  Experiment #12,483
  Hypothesis:  Adaptive ATR works better in volatile markets
  Result:      Rejected
  Reason:      Overfit to 2019–2021
  Evidence:    Sharpe collapsed 2.4 → 0.6 out-of-sample; PBO 0.71
  Confidence:  94%
  Next:        1. Normalise ATR  2. Try volatility clustering  3. Test on commodities
  ```
  **The "Next" section is mandatory** — it is what makes the system self-propelling.
- **Live agent activity** is observable from the moment concurrency exists (Implementation_Plan Stage 4a), not deferred to the full dashboard.

---

## 17. Safety & Risk Controls

- **Two mandatory human gates:** research→paper, paper→live. **No code path may bypass them.**
- **Agents have no trading credentials.** Order placement is a separate, minimally-scoped service. An agent *recommends*; only the execution service, gated on a human-approved record, can act.
- **Hard risk limits enforced outside strategy logic:** per-strategy max loss, per-portfolio max drawdown, position limits, kill switch. These must make a 100% drawdown **structurally unreachable**.
- **Live capital ramps in stages** (1–5% → scale up), never straight to full allocation.
- **Rollback:** any promoted strategy can be demoted or halted from the dashboard immediately.
- **Sandboxed code execution.** A2 writes code that will be executed; it runs isolated, with no network and no credentials.

---

## 18. Technology Choices

| Concern | v1 | Later |
|---|---|---|
| Language | Python 3.11+ | same |
| Array maths | NumPy + Polars | same |
| Path-dependent loops | Numba `@njit` | same |
| Analytics over Parquet | DuckDB | same |
| Parallelism | `multiprocessing` / `joblib` across folds and replications — **not threads** (§9.3) | Distributed workers |
| Reasoning | Claude Code (stateless sessions) | same |
| Strategy code history | git, one branch per strategy (§5.2) | same |
| Metadata DB | SQLite, 3 tables to start (§2.5) | PostgreSQL |
| Job queue | none in nanoAQRL; SQLite + leases with the scheduler | Redis |
| Columnar data | Parquet + DuckDB | + object storage |
| Vector search | Local (FAISS / sqlite-vss) | Dedicated vector DB |
| Scheduling | `scheduler.py` tick loop | Prefect / Airflow if warranted |
| Isolation | subprocess | Docker → Kubernetes |
| Dashboard | Local web app, read-only | same, hosted |

Deliberately boring. **The novelty budget is spent on the research loop, not the infrastructure.**

---

## 19. Non-Functional Requirements

| Requirement | Target |
|---|---|
| **`evaluate.py` runtime** | Seconds, not minutes (§9.1) |
| **Determinism under parallelism** | Bit-identical `honest_score` on re-run (§9.5) |
| **Experiment reproducibility** | 100% from the stored record alone |
| Fold-level parallelism | Scales with cores; no shared mutable state |
| Scheduler recovery | Resumes cleanly after `kill -9`; no orphaned RUNNING jobs beyond lease TTL |
| Idempotency | Re-running any job produces no duplicate state |
| Backwards compatibility | Old experiments remain readable after schema migration |
| Laptop viability | The full v1 loop runs on a single consumer laptop |
| Horizontal scale | Adding workers requires zero agent-logic changes |

---

## 20. Open Technical Questions

**Resolved 2026-07-28**
- [x] ~~Confidence level on the haircut~~ — **`1.65×SE` (~95% one-sided)**
- [x] ~~Bar values~~ — **min score 0.50 · max OOS DD 15% (20% crypto) · min trades 100**
- [x] ~~Train window~~ — **all three (1/2/3 yr) evaluated, best reported, `N_trials` ×3** (§8.2)
- [x] ~~Does the agent tune parameters?~~ — **yes; train-only inside each fold, and it does *not* inflate `N_trials`** (§8.6)
- [x] ~~Trial-counting scope~~ — **per family, per §8.6's table**

**Owner: human — still open**
- [ ] **Survivorship handling for NIFTY-50** — point-in-time index membership, trade the index instead, or accept and document the bias. Blocks trustworthy equity results (§20.1)
- [ ] How is **breadth** measured — instrument count, % of universe profitable, or something else? Needed before it can carry a number
- [ ] How is **complexity** measured — operator count, free parameters, DAG depth? Needed for the tertiary criterion
- [ ] Vault composition — which years, instruments or markets are locked, and the per-family peek budget
- [ ] Broker selection, and whether paper trading is an internal simulator or a broker API

**Design questions**
- [ ] Autocorrelation correction — required from the start, or only once overlapping-position strategies appear?
- [ ] Minimum fold count before a score is considered meaningful
- [ ] Which Monte Carlo variant is canonical (trade-order shuffle, block bootstrap, synthetic path generation)?
- [ ] Null-world generator: which null models, how many replications, and what counts as a "discovery"?
- [ ] Near-duplicate spec detection: exact hash only, or embedding-similarity threshold?
- [ ] **How is `evaluate.py` isolated in practice** — separate process, container, or Unix file permissions under a different user? The design requires "cannot read"; the mechanism is unchosen
- [ ] How is the operator library versioned against in-flight experiments?
- [ ] Vector index choice for v1
- [ ] Chunk size / section detection for documents with poor structural markup
- [ ] Does the Librarian run continuously or in scheduled batches, and how is its compute budget capped?
- [ ] At what branch count does one-branch-per-strategy need a lighter ref namespace?
- [ ] Sub-minute fidelity — at what timeframe do we stop trusting bar-based fills entirely (§6.4)?

### 20.1 Known bias, currently unmitigated ⚠️

**NIFTY-50 survivorship.** The universe is the NIFTY-50 and no delisted-stock or point-in-time membership dataset exists yet. Backtesting today's constituents over 2000–2025 implicitly assumes foreknowledge of which companies would still be index-worthy in 2026 — index membership turns over roughly 2–5 names per year, and every dropped company is invisible.

This inflates every Indian equity backtest, and it is precisely the class of self-deception P0's survivorship check and the null-world calibration exist to prevent. **Until resolved, Indian equity results must be treated as optimistic and must not be promoted to live capital.** Index-level research (NIFTY futures/ETF) is unaffected and can proceed.

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial document — execution model, the single-`evaluate.py` decision with Market/Timeframe profile factoring, provenance hashing, validation battery, operator library, knowledge subsystem, safety controls. |
| 2026-07-27 | Added nanoAQRL (v1 file shape and permission boundary), the honest score, `program.md` required contents, performance and parallelism, and adversarial integrity. Evaluator became unreadable as well as unwritable. |
| 2026-07-27 | Walk-forward resolved — rolling scheme, 1-year test windows, concatenated folds, scheme selection identified as a hidden multiple-testing channel. Librarian formalised. |
| 2026-07-28 | Train window fixed as configurable 1/2/3 years with test always 1 year, folded into `wf_config_hash`. Clearing the bar became an immediate stop. Git branching and merge convention added. |
| 2026-07-28 | **Full rewrite for clarity and consistency.** Collapsed the patched §2A/§4A/§4B/§8A numbering into sequential sections 1–20; merged all superseded rules into their final form; removed duplicated material between the honest score, walk-forward and validation sections; consolidated the changelog. No decisions changed in this pass. |
| 2026-07-28 | **Design decisions locked in.** `z` = 1.65; bar values set (min score 0.50, max DD 15% / 20% crypto, min trades 100). Walk-forward now evaluates **all three train windows and reports the best**, with `N_trials` ×3 so the deflated Sharpe absorbs the selection (§8.2). Parameter tuning enabled, with §8.6 defining precisely what does and does not count as a trial — train-only tuning does not inflate the haircut, but raises internal fold overfitting instead. Cost models re-keyed on `(market, asset_class)` with NSE delivery costs derived (§6.3a). Timeframe range set to 1 second–1 month with an explicit fidelity warning below 1 minute. Added §4.5 continuous operation and the idle-by-design vs idle-by-bug distinction, and §20.1 recording the unmitigated NIFTY-50 survivorship bias. |
