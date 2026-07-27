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
code_version             # git commit of the strategy code
data_snapshot_id         # exact dataset version used
operator_library_version
```

**Why:** the day cost assumptions change — and they will — we must instantly answer *"which of my 40,000 stored results are still comparable?"* Without this, the knowledge base silently mixes results scored under different rules. That is precisely the rot that kills long-running research systems.

Changing any profile or the engine bumps a version. Old results are not deleted; they are marked **incomparable** to the new version and optionally queued for re-evaluation.

---

## 4A. The Honest Score — our `val_bpb` ★

**Unresolved, and it blocks everything else.** No amount of loop engineering compensates for scoring the wrong thing; a bigger, faster loop on a dishonest score just produces wrong answers more efficiently.

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

Starting candidate for evaluation: an out-of-sample, cost-inclusive, deflated risk-adjusted return computed on purged and embargoed splits. To be settled in a dedicated session.

### 4A.3 Ranked criteria

Mirrors the reference's primary/secondary/tertiary structure (PRD §13.3): **primary** the honest score; **secondary** a resource constraint (capacity or turnover — the analogue of his VRAM ceiling); **tertiary** simplicity, scored rather than left to reviewer judgment.

### 4A.4 The invariant

The reference achieves comparability with a single constant — the 5-minute wall clock — rather than a versioning scheme. The AQRL analogue is a **fixed evaluation contract**: data slice, cost model, and test protocol held constant across a campaign. §4.7 provenance hashing enforces this; the design goal is to keep the contract simple enough that it rarely changes, because every change partitions the result history.

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
| Experiment reproducibility | 100% from stored record |
| Scheduler recovery | Resumes cleanly after kill -9; no orphaned RUNNING jobs beyond lease TTL |
| Idempotency | Re-running any job produces no duplicate state |
| Backwards compatibility | Old experiments remain readable after schema migration |
| Laptop viability | Full v1 loop runs on a single consumer laptop |
| Horizontal scale | Adding workers requires zero agent-logic changes |

---

## 14. Open Technical Questions

- [ ] **★ The honest score (§4A.2) — blocks everything downstream**
- [ ] Null-world generator: which null models, and how many replications for a stable FDR estimate?
- [ ] Vault composition — which years, instruments, or markets are locked, and what is the per-family peek budget?
- [ ] How is `evaluate.py` isolated in practice so the agent cannot read it (separate process, container, or file permissions)?
- [ ] Walk-forward window sizing policy per timeframe — fixed bars, expanding, or anchored?
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
