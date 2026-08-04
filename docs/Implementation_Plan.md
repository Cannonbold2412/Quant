# Implementation Plan — AQRL

> **Status:** **Stages 0–9 are built** (nanoAQRL, Foundations, Operator Library, `evaluate.py` productionised, the Nervous System job queue/scheduler, A2 the Quant Engineer, A3 the Research Reviewer, A1 the Research Scientist, A4 the Promotion Committee, A5 the Knowledge Manager, the Human Gates). Stage 4a and Stages 10–13 not started.
> **Last updated:** 2026-08-05
> **Companion docs:** `PRD.md` (why) · `TRD.md` (how) · `Backend-Schema.md` (data) · `App-Flow.md` (sequences)

---

## 0. Build Philosophy

**Build the funnel before the factory.** The bottleneck in quant research is not generating ideas — it is reliably distinguishing genuine alpha from overfitting. The validation engine is built first; the idea generator last. A lab that generates 10,000 hypotheses with a weak evaluator is worse than useless: **it manufactures false confidence at scale.**

**Start smaller than feels right.** The reference project runs a complete autonomous research loop in three files. Stage 0 mirrors that. Every stage after it must be triggered by a **pain actually felt** — the loop plateaus, a question arrives that `results.tsv` cannot answer, one agent visibly fails at one job. Never by the plan alone. These six documents describe a good destination and a bad starting point.

**Every stage produces standalone value.** No stage exists purely as scaffolding. If we stop after Stage 3, we still have a rigorous evaluation harness worth using by hand.

**Prove it is honest before making it fast, and prove it works before making it big.** Correctness → calibration → throughput → scale, in that order.

---

## 1. Dependency Graph

```
Stage 0   nanoAQRL + integrity        ★ START HERE
    │       honest score · evaluate.py · null-world · vault · 5 files
    │
Stage 1   Foundations (config, DB, profiles, data layer)
    │
    ├──► Stage 2   Operator Library
    │        │
    ├──► Stage 3   evaluate.py, productionised   ◄──┘   ★ CRITICAL PATH
    │        │
    │        ▼
    ├──► Stage 4   Job queue + scheduler
    │        │
    │        ▼
    │    Stage 4a  Observability (activity feed + data explorer)  ★
    │        │
    │        ▼
    │    Stage 5   A2 Quant Engineer  ──┐
    │        │                          │  the inner loop
    │        ▼                          │
    │    Stage 6   A3 Research Reviewer ┘
    │        │
    │        ▼
    │    Stage 7   A1 Research Scientist
    │        │
    │        ▼
    │    Stage 8   A4 Promotion + A5 Knowledge
    │        │
    │        ▼
    │    Stage 9   Human gates (CLI first)
    │        │
    │        ▼
    │    Stage 10  Librarian + curiosity engine
    │        │
    │        ▼
    └──► Stage 11  Paper trading + health monitoring
             │
             ▼
         Stage 12  Dashboard (decision layer)
             │
             ▼
         Stage 13  Scale-out
```

**Note the ordering choice:** A2 and A3 (Stages 5–6) come *before* A1 (Stage 7). The iteration loop is the heart of the system and can be exercised with hand-written specs. Building A1 first would mean generating hypotheses with nothing capable of properly testing them.

---

## 2. Stage 0 — nanoAQRL ★ START HERE

**Goal:** a complete autonomous research loop, in five files, whose verdicts are **proven trustworthy** before a single real-data result is believed.

Stages 1–13 remain the destination. None of them begin until Stage 0 has run for real.

### 2.1 The work, in strict order

| # | Task | Notes |
|---|---|---|
| **0.1** | **Implement the honest score** — `SR_oos − 1.65·SE(SR) − SR*(N_trials)` (TRD §7) | Rolling walk-forward, test window fixed at 1 year, **all three train windows (1/2/3 yr) evaluated with the best reported and `N_trials` ×3**, purged, embargo ≥ holding period, 2× costs, per-fold parameter tuning on training data only. All folds **concatenated** into one OOS series. Returns one float |
| **0.2** | **Build `evaluate.py`** around it, with the **hard bar enforced inside it** | Structurally isolated: the agent can neither read nor edit it. The bar gates before any score is computed |
| **0.3** | **Run the null-world test** (TRD §15.3) | Prove the scorer does not invent discoveries in pure noise. Fix and re-run until FDR is low |
| **0.4** | **Build the vault** (TRD §15.2) | Lock the holdout *before* the loop ever touches real data |
| **0.5** | Write `program.md` (contents in TRD §2.4) and `strategy.py`; wire the keep/stop loop | Small, once 0.1–0.4 exist. `program.md` carries the anti-look-ahead rules and the bar's *dimensions*, never the scoring formula or its numbers |
| **0.6** | **Profile, then parallelise across folds** (TRD §9) | Vectorised maths, Numba for path-dependent logic, **processes not threads**. Verify bit-identical determinism before trusting any parallel result |
| **0.7** | **Run it one night. Read every row of `results.tsv` by hand** | The only way to learn what the agent actually does |
| **0.8** | Improve `program.md` from what you saw | Repeat for several weeks |

**Steps 0.1–0.4 are the real work. 0.5 is small. That ratio is the point.**

### 2.2 Deliverables

```
data.py        # snapshots, calendars, costs, universe   — agent: READ ONLY
strategy.py    # the one file the agent edits
evaluate.py    # the harness + hard bar                  — agent: NO READ, NO WRITE
program.md     # instructions + acceptance bar           — human-edited only
results.tsv    # commit | score | n_trades | status | description
```

Plus three SQLite tables (`strategies`, `experiments`, `evaluations`) with `experiments.code_commit` pointing at git. No job queue, no scheduler, no agents beyond the one.

### 2.3 Done when

- **Null-world FDR measured and low** — the pipeline does not manufacture discoveries
- The vault exists and the loop **provably cannot read it**
- The loop runs unattended overnight; every row carries a verdict
- The same experiment re-runs **bit-identical** under parallelism
- The acceptance bar was written down *before* the search started and was **not moved afterwards**

---

## 3. Stage 1 — Foundations

**Goal:** the substrate everything else assumes.

| Deliverable | Notes |
|---|---|
| Project skeleton | `aqrl/` package |
| Config system | Env + file, no secrets in code |
| SQLite schema | Per `Backend-Schema.md`, with migrations from day one |
| DB access layer | Thin repository pattern; **no SQLite-specific SQL** (TRD §20) |
| `MarketProfile` / `TimeframeProfile` loaders | YAML → validated object → content hash |
| **Cost models keyed on `(market, asset_class)`** | Not market alone — NSE charges cash equity, ETFs and futures differently (TRD §6.3). Asset classes: cash equity, ETF, future, CFD, spot crypto, perpetual |
| Timeframe range | Profiles must span **1 second → 1 month**; no hardcoded annualisation anywhere |
| First profiles | Market/timeframe is **not** fixed to one pilot — all six markets are in scope from the start, so profile loading must be generic before any single profile is filled in |
| Data snapshot manager | Ingest Parquet → immutable **raw** snapshot + content hash. Raw is never rewritten |
| **Corporate-action adjustment pipeline** ★ | Source data is **unadjusted** (TRD §15.2). Build: the `corporate_actions` table, back-adjustment applied **at load time** (not persisted), and volume adjusted inversely. Without this every Indian equity backtest is meaningless — a 1:2 split reads as a −50% move |
| **Unexplained-jump validator** | Flag any \|return\| > ~20% with no matching corporate action; a human resolves each before the snapshot is valid (TRD §15.4). This is the only thing that catches *missing* actions |
| **Point-in-time universe resolution** ★ | `index_membership` table + resolution inside `data.py` (TRD §15.3). Requires price history for **all ~100–150 stocks ever in NIFTY-50**, not today's 50 — the ones that left are the invisible losses. **Blocked on data collection**; index-level research proceeds meanwhile |
| Per-market data validators | Run **at ingest**, not at experiment time |
| Structured logging | Correlation IDs threading `strategy → experiment → job` |

**Done when:** a market snapshot can be ingested, versioned, hashed and loaded by ID; profiles resolve and hash deterministically.

> ✅ **Built.** The `aqrl/` package: layered config, canonical content hashing, structured logging with correlation IDs, a migration runner over every `Backend-Schema.md` table, a thin repository layer, YAML→validated→hashed profiles spanning 1 second to 1 month, and the data layer — content-addressed snapshots, load-time corporate-action adjustment, point-in-time universe resolution, and ingest-time validators. `nanoaqrl` now persists through the canonical schema. Exercised via the `aqrl` CLI.
>
> **Still blocked on data, as designed:** point-in-time membership ships as table, resolver, importer and tests, but real snapshots stay `point_in_time_membership = 0` until price history exists for the ~100–150 ever-members of NIFTY-50. Indian equity results must not reach live capital until then.

---

## 4. Stage 2 — Operator Library

**Goal:** the vetted vocabulary strategies are composed from (TRD §11).

| Deliverable | Notes |
|---|---|
| Operator base class + registry | Declares category, parameters with valid ranges, valid markets/timeframes |
| Transformations | Rolling mean, EMA, **JMA**, Kalman, ATR normalisation, fractional differencing, PCA, wavelets |
| Signal operators | Crossover, threshold, breakout, volatility expansion, momentum, mean reversion, volume confirmation |
| Risk operators | ATR stop, time stop, trailing stop, position sizing, Kelly variants, volatility targeting |
| Portfolio operators | Equal weight, risk parity, correlation clustering |
| **Unit tests per operator** | Non-negotiable. An untested operator cannot enter the library |
| Spec DAG format + canonical hashing | Enables duplicate detection (`spec_hash`) |

**Done when:** a strategy spec is expressible purely as an operator composition, hashed canonically, and two logically identical specs produce the same hash.

> ✅ **Built.** `aqrl/operators/`: an `Operator` base class declaring category, parameters with valid
> ranges, and valid markets/timeframes; a registry whose `operator_library_version` is **derived** by
> content hash rather than hand-maintained, so it cannot drift from what it describes; **32 operators**
> across all four categories; the spec DAG with canonical structural hashing; and a compiler turning a
> spec into the `(df, params) → Series` callable `nanoaqrl/evaluate.py` already accepts. Persisted via
> `OperatorRepository.sync` and `SpecRepository.insert_spec` (which rejects duplicates *before* compute
> is spent), and exercised through `aqrl operators` / `aqrl spec`.
>
> **Causality is enforced by construction, not by review.** The causality and contract suites are
> parametrized over the whole registry, so a new operator is covered the moment it is registered and
> cannot enter the library untested. Truncation invariance is checked for every operator at several
> parameter draws, with two deliberately-leaky negative controls proving the scan can actually fail.
> That mattered: it caught a `rolling_sum` bug where a bare `cumsum` let one warm-up NaN empty the
> entire downstream series, and an `atr_stop` bug where a trade opened during ATR warm-up got no stop
> level and never got one afterwards.
>
> **Zero new dependencies.** Rolling PCA uses `numpy.linalg.svd` over the trailing window rather than
> scikit-learn — whose whole-sample fit is precisely the leak TRD §9.4 names — and wavelets are a
> causal trailing-window à trous transform rather than a whole-series DWT, so `PyWavelets` was not
> needed either. Correlation clustering uses `scipy.cluster.hierarchy`, already a dependency.
>
> **Deferred, deliberately:** Numba. TRD §19 earmarks `@njit` for the path-dependent risk operators,
> but §9.6 says profile before optimising. They ship as plain NumPy loops with a clean seam; Stage 3's
> profiling step decides.

---

## 5. Stage 3 — `evaluate.py`, productionised ★ CRITICAL PATH

**Goal:** the single, profile-driven evaluation engine (TRD §6). **The most important code in the system.**

### 5.1 Sub-stages

**3a — Backtest core**
- Profile-driven cost model, fill model, calendar
- Correct annualisation from `TimeframeProfile.periods_per_year` — **a hardcoded constant here is a project-level bug**
- Tradebook + equity curve → Parquet

**3b — The hard bar and Phase 0** *(build before any metric)*
- Bar enforcement: min trades, max OOS drawdown, breadth, 2× cost survival, complexity
- Look-ahead detection (shift/lag verification)
- Data-leakage scan; survivorship / point-in-time checks
- Signal→order→fill ordering validation; NaN/inf guards

> Look-ahead bias is the dominant failure mode of LLM-written strategy code. A P0 failure is a **bug**, routed to A2 — never recorded as a research finding.

**3c — Core metrics (P1, P2)**
Sharpe, Sortino, Calmar, CAGR, max/avg DD, DD duration, profit factor, win rate, expectancy, turnover, exposure.

**3d — Robustness battery (P3)**
- Rolling walk-forward, purged and embargoed, folds concatenated
- Monte Carlo; **deflated Sharpe with correct family-level trial counting**
- White's Reality Check; CSCV/PBO; regime analysis
- Cost sensitivity sweep; parameter sensitivity

**3e — Market-specific gates** (TRD §6.5)
Crypto venue robustness and funding sensitivity; equities survivorship and capacity; commodities roll-method sensitivity; forex session dependence.

**3f — Provenance & reporting**
Stamp engine version, both profile hashes, `wf_config_hash`, snapshot ID, operator library version, seed. Structured JSON report + human-readable markdown.

### 5.2 Validation of the validator — known-answer tests

Before trusting it, `evaluate.py` must be tested against cases whose correct answer is known in advance:

- A deliberately **look-ahead-biased** strategy → P0 must catch it
- A deliberately leaky **vectorised** strategy (unlagged signal, whole-sample normalisation, backfilled NaNs) → P0 must catch it. Vectorisation is the top source of look-ahead (TRD §9.4), so this case is **mandatory**
- A **pure-noise** random strategy → must not pass P3
- A strategy **overfit to one regime** → PBO must flag it
- A strategy with a **real but small edge** → must survive P2, die on realistic costs
- A **known-good published** strategy → results within tolerance of published figures
- The same experiment **single-threaded vs parallel** → bit-identical `honest_score` (TRD §9.5)

**Done when:** all known-answer cases behave correctly, and identical inputs reproduce identical outputs bit-for-bit.

> ✅ **Built.** `aqrl/eval/`: a single panel-native engine (`(n_bars × n_instruments)`, so a
> single instrument is a panel of one) driven entirely by a `ResolvedProfile`, running the ordered
> funnel — complexity → P0 → P1 → P2 → P3 → market gates → the bar — with a failure at any phase
> short-circuiting everything after it. `engine.py` orchestrates; `panel.py`/`fills.py`/`costs.py`/
> `backtest.py` are 3a; `p0.py`/`bar.py` are 3b; `metrics.py` is 3c; `walk_forward.py` plus
> `stats/{honest_score,deflated,monte_carlo,reality_check,cscv}.py` and `regimes.py` are 3d;
> `gates/{equities,crypto,commodities,forex}.py` are 3e; `report.py`/`persistence.py`/`version.py`
> are 3f. Persists into the schema Stage 1 already migrated — no new migration was needed.
>
> **One statistical layer, not per-market forks (TRD §6.1).** The honest score and walk-forward
> protocol moved out of `nanoaqrl/_lib/` into `aqrl/eval/`; `nanoaqrl/_lib/honest_score.py` and
> `walk_forward.py` are now thin re-export shims, so Stage 0's loop runs unchanged against the one
> canonical implementation rather than a diverging copy.
>
> **The known-answer suite passed — M1 cleared.** All seven §5.2 cases, run through the actual
> engine funnel: a `shift(-1)` look-ahead and a whole-sample-normalised, backfilled **vectorised**
> leak are both caught at P0 (the vectorised case mandatory per TRD §9.4); pure noise fails before
> or at P3 across every seed tried; a strategy overfit to one regime collapses `wf_efficiency` well
> below 1.0 (TRD §8.6's own diagnostic — PBO itself proved too noisy with few folds to gate on
> reliably, and is reported rather than asserted on for that reason); a small genuine edge survives
> 1× costs and dies exactly at the 2× default; Sharpe, max drawdown and round-trip cost all match
> closed-form derivations; and single-threaded vs 4-worker runs, and repeated runs of the same
> experiment, are bit-identical. The "known-good published strategy" case is replaced by the
> analytic ground-truth case — no real price history exists yet (§21) — and stays open below.
>
> **Profiled, then optimised on evidence, per §9.7.** A 20-year, 5-instrument, 500-replication
> evaluation ran at 4.29s; `cProfile` placed 46% of it in the Monte Carlo stationary-bootstrap
> index walk and 24% in an O(n²) expanding-median recomputation inside regime labelling — the rest
> (spec evaluation, all three walk-forward windows) was already fast. The bootstrap walk is
> exactly TRD §9.2's "genuinely path-dependent logic," so it is the one place Numba lands, applied
> only after profiling named it (Stage 2 left the seam open for precisely this decision); the
> expanding median was an algorithmic bug, not a JIT candidate, and became a proper O(n log n)
> two-heap running median instead, verified numerically identical to the O(n²) version it
> replaced. Together: 4.29s → 0.92s, a 4.7× reduction, entirely evidence-driven.
>
> **Determinism (TRD §9.5) is real, not asserted.** Fold-level parallelism runs through
> `parallel.map_ordered` — a process pool at the fold level, submission-ordered regardless of
> completion order — and 1 vs 4 workers on the same experiment produce a bit-identical
> `honest_score` and an identical concatenated OOS series, checked directly rather than assumed.
>
> **Stated, not papered over:** only `nse_equity` × `daily` has a shipped profile, so the crypto/
> commodities/forex gates are implemented generically and exercised against synthetic profile
> variants — real profiles land with the markets (§21). Breadth and capacity/ADV are provisional,
> measured-and-reported defaults pending the human-owned definitions §21 asks for. Market gates and
> the breadth bar item are computed from the full-history P2 backtest rather than fold-by-fold,
> since the walk-forward's per-instrument breakdown collapses to one portfolio series by design.
> `experiments.failure_reason` has no dedicated "data-provenance rejected" value, so a P0 rejection
> on adjustment/vault/point-in-time grounds is recorded as `code_error`, the closest existing bucket.

---

## 6. Stage 4 — Nervous System ✅ built

**Goal:** the coordination layer (TRD §4).

| Deliverable | Notes |
|---|---|
| `jobs` table + queue API | Atomic claim with lease + heartbeat |
| `scheduler.py` | Single always-on tick loop, ~60s |
| Worker dispatch | Subprocess, isolated, reaped with cost accounting |
| State machine | Enforces valid transitions; invalid transitions are errors, not warnings |
| Event → job mapping | Per TRD §4.1 |
| Budget enforcement | Tokens, iterations, experiments, compute — with back-pressure |
| Failure classification | Transient (retry) vs deterministic (route to fix) |
| Quarantine logic | Poison-pill protection after *k* consecutive failures |
| Per-experiment time budget | Overrun → killed, recorded as `crash` |
| Crash recovery | Lease expiry returns orphaned jobs; `kill -9` loses nothing |

**Done when:** the scheduler survives `kill -9` mid-job with **zero state loss and zero duplicated work.**

> **Built as `aqrl/orchestration/`** — `JobRepository` (atomic `BEGIN IMMEDIATE` claim,
> lease/heartbeat, `dedupe_key`), `states.transition` (explicit tables for `strategies`/
> `experiments`, raises `InvalidTransition`, writes `audit_log`), `events.emit` (the TRD §4.1
> table, one transaction per state-change-plus-enqueue), `budgets` (global day-scoped caps
> gate dispatch; per-strategy/goal tracked but not yet gated — nothing spends against them
> until Stage 5's agent calls exist), `failures` (transient/deterministic classification,
> exponential backoff, quarantine after *k* consecutive failures), `worker.py` (one job, one
> subprocess, split `run`/`persist` so a multi-minute evaluation never holds SQLite's write
> lock), `dispatch.Dispatcher` (spawn/reap, per-experiment time budget via SIGTERM→SIGKILL),
> and `scheduler.tick` (App-Flow §13's loop, plus TRD §4.5 idle-cause reporting). Two
> deliberate scope cuts: only the `EVALUATE` handler is registered — `NULL_WORLD_RUN` and
> `GENERATE_REPORT` are real future handlers, not stubs, so an unregistered `job_type` fails
> loudly (`NotImplementedHandler`, classified deterministic) rather than half-working; and
> `TIME_DRIVEN_SCHEDULE` (TRD §4.2) is mechanism-only and empty, since every cadence row needs
> a Stage 5+ producer. A worker-killed time-budget overrun is recorded on the `jobs` row
> (`error_message`, `failure_class`); `experiments.failure_reason` has no timeout/infra bucket,
> so the experiment itself is simply left open for the retried attempt to close, the same
> "closest existing bucket" tradeoff Stage 3 made for P0 provenance rejections above.

---

## 7. Stage 4a — Observability ★

**Goal:** watch the machine work, without a terminal. **Distinct from the Stage 12 dashboard** — this exists to debug the system, not to make decisions, and it ships the moment there is concurrency worth watching rather than waiting for candidates worth reviewing.

| Deliverable | Notes |
|---|---|
| Activity feed | Live view over `jobs`: agent, target strategy/experiment, status, duration, cost (UI-UX-Brief §8.1) |
| Data explorer | Read-only table browser + row viewer with drill-down links + raw SQL box (UI-UX-Brief §8.2). **No write path, ever** |
| Polling exception | Only the activity feed auto-refreshes; everything else stays manual-refresh |

**Done when:** a human can tell what every agent is doing right now, and inspect any row in the database, without opening a SQLite client or grepping logs.

**Why here and not Stage 12:** once the scheduler dispatches to more than one agent, a terminal alone stops answering *"why is this stuck?"* — that pain is felt at Stage 4–5, not after strategies start reaching human review.

---

## 8. Stage 5 — A2 Quant Engineer ✅ built

**Goal:** spec → working code.

| Deliverable | Notes |
|---|---|
| Claude session wrapper | Stateless, structured output, schema-validated |
| Context assembler | **Python builds the brief** — spec, research plan, prior code + diff, prior evaluation, operator catalog |
| Branch creation | On a strategy's first `IMPLEMENT` job: create `strategy/<strategy_id>` (TRD §5.2) |
| Code generation | Emits a strategy module composed from operators |
| Sandboxed execution | No network, no credentials (TRD §18) |
| Static check pipeline | Compile, lint, look-ahead scan — before evaluation is even queued |
| `FIX_CODE` path | Deterministic failures return with diagnostics attached |
| Prompt versioning | Recorded on every output |

**Done when:** given a hand-written spec, A2 produces code that passes P0 and runs through `evaluate.py` unattended.

> **Built as `aqrl/agents/` + `aqrl/orchestration/handlers/implement.py`.** The
> decisive design call: `aqrl/eval/engine.py` compiles **specs**, not source
> (`compile_spec(inputs.spec, ...)`), so A2 never emits freeform Python the
> engine then executes — it emits a schema-validated spec (`ProposedSpec`,
> Stage 2's operator DAG plus a plain-language `change_summary`), and
> `render.py` deterministically renders that into
> `strategies/<uid>/strategy.py`. The guardrail is `spec_hash` equality
> between the rendered module and the stored row — checked on every render,
> not just in tests — which makes "the code" and "the thing `evaluate.py`
> scores" structurally the same object (TRD §6.1's no-forking rule, applied
> to codegen). **Iteration 1, from a hand-written spec, needs no LLM call at
> all** — pure Python render → sandboxed static check (`sandbox.py`, a
> scrubbed-env, resource-limited, timed-out subprocess reusing Stage 3's own
> `static_lookahead_scan` / `spec_absolute_level_scan` / truncation-invariance
> checks) → an idempotent, content-addressed git commit
> (`aqrl/vcs.py`, always an orphan branch per strategy — "two unrelated
> hypotheses share no content") → `code_versions` insert → `EVALUATE`
> enqueued with the eval-relevant payload carried through unchanged. Claude
> (`agents/session.py`'s `AnthropicSession`, via `client.messages.parse` for
> schema-validated structured output) is invoked only for a plan-driven
> iteration or a `FIX_CODE` retry — `StubSession`/`ReplaySession` stand in for
> every test, so nothing in the suite needs `ANTHROPIC_API_KEY`. A failing
> static check is a bounded-retry loop (`max_fix_attempts`, default 3,
> counted from `code_versions` rows on the experiment) ending in
> `strategies.quarantined`, not a failed job — the checks ran and produced a
> verdict, the same shape as `EVALUATE`'s own bar-clear short-circuit. A3
> (Stage 6) doesn't exist yet, so the plan-driven path is exercised by
> inserting a `research_plans` row by hand and enqueuing `IMPLEMENT`
> directly — done-when is proven both via direct handler calls and, for the
> hand-written-spec path, through a real claimed job running in a real
> `aqrl.orchestration.worker` subprocess. One deliberate scope note: sandbox
> isolation is process-level (`subprocess` + `resource` limits), not
> container-level (TRD §19 defers Docker) — it blocks credential/database
> access and runaway CPU/memory, not a generated process opening a socket.

---

## 9. Stage 6 — A3 Research Reviewer ✅ built ★ CLOSES THE LOOP

**Goal:** the iteration engine — the first moment AQRL is more than a pipeline.

| Deliverable | Notes |
|---|---|
| **Bar-clear short-circuit** ★ | The worker checks `bar_result` before invoking A3 at all — a pass routes straight to `PROMOTE`, skipping A3 entirely (App-Flow §6.1) |
| Context assembler | Spec, **full** iteration history, `bar_failed_on` + raw diagnostics, regime breakdown, related knowledge |
| Verdict logic (below the bar only) | `iterate` / `plateau` / `reject` — **no `promote` verdict**; the only path to A4 is the short-circuit above |
| Research plan output | **Plain-language changes, never code** (PRD §6.1) |
| Stop-condition evaluation | Checked in Python *before* invoking Claude, so budget is never wasted |
| Plateau detection | **5 consecutive bar failures** — never a score comparison, since there is only ever one passing evaluation per strategy |
| Plateau routing | Always → A5, `failure_reason = plateaued_below_bar`. Never A4 |
| Loop wiring | (below bar) A3 → A2 → evaluate → A3. (bar cleared) evaluate → A4 directly |

**Done when:** a hand-written spec runs autonomously through several below-bar iterations, then **stops the instant one clears the bar** — without attempting a further iteration to chase a higher score — **and** a deliberately-stuck spec plateaus at 5 bar failures and routes to A5.

> **This is the first real milestone.** At this point the system iterates on research without a human. Everything before it is infrastructure; everything after it is amplification.

> **Built as `aqrl/orchestration/handlers/review.py`**, alongside A2's
> `implement.py`, same `run`/`persist` split for the same reason (Claude is
> never called inside an open write transaction). `agents/session.py` gained
> `ProposedPlan`/`ReviewSession` — A3's one output shape, mirroring
> `ProposedSpec`/`AgentSession` — and `agents/context.py` gained
> `assemble_review_brief`, whose redaction is deliberately *looser* than A2's
> on one axis: a bar failure has no `honest_score` to protect (TRD §7.5), so
> the brief includes the raw `bar_failed_on` value/threshold pair A3 actually
> needs to tell "getting closer" from "stuck" (App-Flow §6.3), while still
> withholding the engine itself. Stop conditions (plateau patience, the hard
> iteration cap, and now a per-strategy token/iteration budget) are checked
> in `run()` before any session call; `handlers/review.py` is the first real
> consumer of the per-strategy `budgets` rows Stage 4 only ever tracked
> (`aqrl/orchestration/budgets.py`). Two deliberate scope cuts, both following
> Stage 5's own precedent for `PROMOTE`: the `ARCHIVE` job a `plateau`/`reject`
> verdict enqueues has no handler — A5 is Stage 8 and does not exist yet, so
> it fails loudly (`NotImplementedHandler`) rather than half-working, and the
> strategy's own terminal state (`plateaued`/`rejected`) is already correct by
> the time that job is enqueued.
>
> **Two bugs found and fixed on the way, both load-bearing for this stage to
> be reachable at all.** `handlers/evaluate.py`'s follow-on `emit()` was
> dropping the incoming job payload (`asset_class` and friends) — harmless
> while every loop was one iteration long, fatal the moment a second
> `IMPLEMENT`/`EVALUATE` round has to happen, which is this stage's entire
> premise. And `eval/engine.py`'s P3 bar-failure branch was routing through
> the phase-agnostic `_failed()` helper, which hardcodes `bar_verdict=None` —
> correct for P0-P2 (there is no bar_verdict yet) but wrong for an actual bar
> failure, since `handlers/evaluate.py`'s PROMOTE/REVIEW routing reads
> `report.bar_verdict` directly. Left unfixed, no real bar failure could ever
> have enqueued a `REVIEW` job in the first place — masked until now by a
> known-answer test whose own bar-failure assertion turned out to be
> vacuous (the scenario it built never actually reached the bar). Fixing it
> surfaced a second, related gap: `BarVerdict.failed_on`'s vocabulary
> (`min_trades`, `max_drawdown`, `cost_stress`, `breadth`) was never mapped to
> `experiments.failure_reason`'s own CHECK-constrained enum, so persisting a
> real bar failure raised a SQLite `IntegrityError` before this stage
> existed to test the case. `BAR_FAILURE_TO_EXPERIMENT_REASON`
> (`aqrl/eval/bar.py`) is the map, using the same "closest available bucket"
> tradeoff `implement.py`'s P0-provenance handling already established for
> `max_drawdown`/`breadth`, which have no dedicated slot in the schema.
>
> **Done-when, proven two ways.** `tests/orchestration/test_review_handler.py`
> exercises every guard, stop condition, and verdict directly. The actual
> loop — claim, run, persist, repeat, through the real job queue and
> `aqrl.orchestration.worker.run_job`, no direct handler calls —
> is `tests/orchestration/test_loop.py`: a filter-gated crossover spec (real
> edge, real P0-P2 pass, tunable trade count) run against one fixed
> synthetic snapshot proves both halves of the done-when — several below-bar
> `iterate` verdicts followed by an immediate stop the instant one clears the
> bar, and, separately, five consecutive bar failures forcing a `plateau`
> with zero further LLM calls.

---

## 10. Stage 7 — A1 Research Scientist

**Goal:** autonomous hypothesis generation.

| Deliverable | Notes |
|---|---|
| Research Brief assembler | Relevance search (vector + structured filters) over both knowledge bases — **not a table scan** (App-Flow §3.2) |
| Spec generation | Operator composition + parameter ranges + rationale + `expected_behavior` |
| Duplicate rejection | `spec_hash` check **before** any compute is spent |
| **Anti-amnesia injection** | Surfaces contradicting lessons; A1 must justify overriding them |
| Dual-source traceability | `source_external_knowledge_ids` + `source_internal_knowledge_ids` |
| Portfolio allocation | Honours the 70/20/10 split (PRD §4.5) |
| Batch + event triggers | Nightly batch, curiosity closure, and high-novelty push |

**Done when:** A1 generates novel, non-duplicate specs that respect known failure patterns, and the full A1→A2→evaluate→A3 loop runs end to end unattended.

> ✅ **Built** (not previously recorded in this document — added retroactively
> alongside the Stage 8 write-up below, so the status header is accurate).
> `aqrl/agents/research_brief.py` (embedding-cached relevance search, bounded
> to a recency-ordered candidate window before anything is embedded — "not a
> table scan"), `agents/context.py`'s `assemble_generate_brief`, and
> `orchestration/handlers/generate.py`. Exact-duplicate rejection is
> `SpecRepository.by_hash` before any compute is spent; a structural near-
> duplicate proceeds but is recorded to the audit log. Proven end to end by
> `tests/orchestration/test_loop.py::test_a1_generates_a_spec_that_flows_through_the_unmodified_loop` —
> a `GENERATE_SPEC` job through the real queue produces a strategy identity
> and spec that flows through `IMPLEMENT`/`EVALUATE`/`REVIEW` unmodified.

---

## 11. Stage 8 — A4 Promotion + A5 Knowledge

**Goal:** close the learning loop.

### A4 — Promotion Committee
- Context = the **entire** research history, including every bar-failing attempt
- Explicit weighing of iteration count as an overfitting signal
- Capacity/liquidity check — can *this strategy alone* trade at real size
- **No portfolio-correlation check** ★ — out of scope for v1 (PRD §3); correlation is a dashboard-computed value the human sees, never part of A4's brief
- Outputs a recommendation record; **no credentials, no execution authority**

### A5 — Knowledge Manager
- Runs **once per strategy**, over the complete iteration set (TRD §12.1)
- Lab notebook with **mandatory non-empty next-questions**
- Knowledge entries with evidence **and counter-evidence** counts
- Knowledge graph edges, **every edge backed by experiments**
- Weekly cross-experiment pattern mining → family-scoped rules
- Research questions pushed to the curiosity queue

**Done when:** rejected experiments demonstrably prevent similar future proposals — measured by the **repeat-failure rate** trending toward zero.

> The second real milestone: the lab stops being a loop and starts being a **memory**.

> ✅ **Built.** `aqrl/orchestration/handlers/promote.py` (A4) and
> `aqrl/orchestration/handlers/archive.py` (A5, serving both `ARCHIVE` and
> `MINE_PATTERNS` — one module for both, mirroring `implement.py`'s
> `IMPLEMENT`/`FIX_CODE` split), plus new `PromotionRepository`,
> `KnowledgeEdgeRepository`, `LabNotebookRepository`, and write paths on
> `KnowledgeEntryRepository`/`ResearchQuestionRepository`. No new migration —
> every table Stage 8 writes (`promotions`, `knowledge_entries`,
> `knowledge_edges`, `lab_notebooks`, `research_questions`) already existed
> from Stages 1/3. `ProposedPromotion`/`ProposedKnowledge` join
> `agents/session.py`'s existing `Proposed*` family — same
> `extra="forbid"`, same Stub/Replay pair, same lazy `AnthropicSession`
> default, so nothing in the suite needs `ANTHROPIC_API_KEY`.
>
> **A4 is a judge, not a producer** — the one deliberate asymmetry in
> `agents/context.py`'s redaction scheme. Every other brief in the system
> withholds `honest_score` and the engine internals (App-Flow §4.1); A4's
> brief includes them, because nothing A4 writes ever reaches a future
> spec-shaping agent — it emits a recommendation a human confirms, full
> stop. `persist()` hardcodes `requires_human_approval=1` on every decision
> regardless of what A4 said, so `approve` can never be self-certifying
> (App-Flow §7.3). `reject` and `defer` are A4's own authority: `reject`
> transitions the strategy to `rejected` — a deliberate deviation from
> Backend-Schema §14.1, which only draws that edge off the never-cleared-
> the-bar branch, recorded in `states.py`'s docstring alongside the existing
> quarantine note, the same closest-available-bucket precedent Stages 3/5/6
> already set. `defer` leaves status untouched (PRD §9.2 already stopped
> this strategy the instant it cleared the bar) and opens a fresh
> `research_goals` row instead — `created_by` left `NULL` there, since the
> column's CHECK constraint only allows `'human'`/`'agent5'` and neither fits
> A4.
>
> **A5 gets A3's asymmetric treatment, for the opposite reason A4 doesn't.**
> A5's output *does* reach a future A1 brief
> (`KnowledgeEntryRepository.failure_patterns`, consumed by
> `assemble_generate_brief`) — a live reward-hacking channel — so
> `assemble_archive_brief`/`assemble_mine_brief` withhold the full forbidden-
> term list `honest_score` included, reusing the same raw
> `bar_failed_on`-plus-diagnostics shape A3's brief already established.
> `ARCHIVE` is idempotency-guarded on a `lab_notebooks` row already existing
> for the strategy (TRD §12.1: A5 runs once, ever) and closes every one of
> the strategy's experiments into the `archived` terminal state
> `EXPERIMENT_TRANSITIONS` defined at Stage 4 and nothing before this stage
> ever reached. `MINE_PATTERNS` is Stage 8's first live entry in
> `scheduler.TIME_DRIVEN_SCHEDULE` — the mechanism Stage 4 built and left
> empty specifically for this.
>
> **The repeat-failure rate is a measured, decided quantity, not an
> aspiration.** `db.repositories.knowledge.repeat_failure_rate` defines a
> repeat as: an experiment's `failure_reason` was already documented — in
> `knowledge_entries.evidence.failure_reasons`, a small structured field
> `handlers/archive.py` populates on every entry it writes — by a non-
> superseded, market/timeframe-applicable entry created before that
> experiment's own spec. Matching against the structured field rather than
> free-form `statement` prose was a deliberate choice: `statement` is
> deliberately prose ("ATR multipliers above 3.0 consistently overfit"), and
> matching against it would have been a fragile substring guess. Exposed via
> `aqrl knowledge rate [--since]`.
>
> **Proven two ways**, matching Stage 6's own precedent.
> `tests/test_knowledge_repositories.py` and `tests/orchestration/
> test_promote_handler.py`/`test_archive_handler.py` exercise every guard,
> transition, and the edge-upsert/mandatory-non-empty contracts directly.
> `tests/orchestration/test_memory_loop.py` drives the actual done-when
> through the real job queue and `aqrl.orchestration.worker.run_job` —
> unmodified `GENERATE_SPEC`→`IMPLEMENT`→`EVALUATE`→`REVIEW`→`ARCHIVE` for a
> spec that fails the bar, A5's resulting lesson visible through the exact
> reader a real A1 call would use, and a second, later strategy failing the
> identical way counted by `repeat_failure_rate` as the preventable repeat
> it is. The "a lesson written too late never counts, and an unrelated
> failure never counts" halves of the mechanism are unit-tested directly
> against the metric function rather than re-run through the queue a second
> time.
>
> **One regression caught and fixed on the way.** Populating
> `TIME_DRIVEN_SCHEDULE` for the first time broke three Stage 4/6 tests that
> assumed it was permanently empty — two `test_scheduler.py` cases asserting
> `tick()` dispatched nothing now saw the newly-firing `MINE_PATTERNS` job
> too, and `test_dispatch.py`'s `NotImplementedHandler` fixture had used
> `ARCHIVE` as its example of "a valid job_type nobody services yet," which
> stopped being true. Fixed by isolating the dispatch-mechanics tests with
> an explicit empty `schedule=[]` and swapping the fixture to
> `COLLECT_PAPERS` (still unimplemented, Stage 10's concern) — the kind of
> shared-state regression `states.transition`'s "errors, not warnings" rule
> exists to surface loudly rather than let slide.
>
> **Known limits, stated rather than papered over.** The repeat-failure
> metric is measurable, not yet minimized — the loop test proves the
> mechanism works with a stubbed A1, not that a live A1 actually heeds a
> shown lesson; the trend to zero is an operating observation over real
> nights (M4). `promotions.merge_commit` stays NULL — merge-on-approve is
> Stage 9 (TRD §5.3). A4's capacity verdict is only as good as
> `GateContext.assumed_capital`, still Stage 3's provisional default.
> `MINE_PATTERNS` firing weekly against sparse history will legitimately
> find nothing some weeks and write nothing — a correct outcome the handler
> returns cleanly from, not a failure it forces a pattern to avoid.

---

## 12. Stage 9 — Human Gates (CLI first)

**Goal:** the safety boundary, before any UI work.

| Deliverable | Notes |
|---|---|
| `aqrl review` CLI | Lists pending decisions, renders the full evidence package |
| Approve/reject with **mandatory typed note** | Recorded to `promotions.human_notes` |
| Structured rejection reasons | Flow into A5 as knowledge |
| Deployment record creation | Baseline `expected_*` copied from validation |
| **Merge-on-approve** | Approval merges `strategy/<id>` into `deploy/paper` or `deploy/live`; the merge commit references `promotions.uid` (TRD §5.3) |
| Vault access gate | Opening the vault is logged and decrements the family budget (TRD §15.2) |

**Done when:** a human can make a fully-informed gate decision from the terminal. The dashboard is deferred — a CLI is sufficient to validate the loop.

> ✅ **Built.** `aqrl/gates.py` (`pending`/`evidence`/`approve`/`reject`) plus
> three new thin repositories — `DeploymentRepository`, `LifecycleEventRepository`,
> `VaultAccessRepository` — added to `db/repositories/promotion.py` alongside the
> existing `PromotionRepository`, since all four tables share one migration
> (`0003_promotion_lifecycle.sql`) and one lifecycle. `StrategyRepo.merge()`
> (`vcs.py`) is the one new git operation. `aqrl review list/show/approve/reject`
> in `cli.py` is a thin argparse shell over `gates.py` — the logic is tested
> without going through `main()`. **No migration** — every column Stage 9
> writes already existed.
>
> **`gates.approve` is the only code path in the system that merges a branch,
> opens the vault, or inserts a `deployments` row** (TRD §18). Git runs
> OUTSIDE the transaction — the same `run`/`persist` split `implement.py`
> uses for `commit_file` — and `StrategyRepo.merge` is idempotent by git's
> own semantics (an already-merged branch just returns HEAD), so a crash
> between the merge and the database write is safe to recover by re-running
> `approve`. Every validation guard (empty note, unknown promotion,
> already-decided, exhausted vault budget) runs and can raise before the
> git-touching import (`vcs.py`, which pulls in `fcntl`) is even reached —
> deliberate, so `pending`/`evidence`/`reject` and every failure path of
> `approve` stay importable and testable on a platform without it.
>
> **Vault: gate only, not App-Flow §15's full scoring flow.** `approve` logs
> the open and decrements the family's lifetime budget (`Settings.
> vault_budget_per_family`, default 1, derived from `vault_access_log`'s own
> row count rather than a second stored counter that could drift from it) —
> it does not load the vault snapshot or re-score the strategy against it.
> `vault_access_log.result_score`/`outcome` stay `NULL`, a known limit rather
> than a shortcut papered over: that needs a vault snapshot that does not
> exist yet.
>
> **Structured rejection reasons reach A5's knowledge base by direct write,
> not by re-running A5.** `ARCHIVE` already fired on `PROMOTION_DECIDED`,
> before the human ever saw this queue, and `archive.py`'s `lab_notebooks`
> idempotency guard makes a second `ARCHIVE` for the same strategy a no-op
> (TRD §12.1: "A5 runs once, ever"). `gates.reject` writes the
> `knowledge_entries` row itself — human-authored, no LLM call — in the same
> `evidence.failure_reasons` shape `repeat_failure_rate` already reads, so a
> human rejection counts toward Stage 8's metric exactly like an
> A5-recorded one (proven in `tests/orchestration/test_human_gates.py`).
> `reason` is validated against `experiments.failure_reason`'s existing
> sixteen-value vocabulary — no new enum — mirroring `archive.py`'s own
> `_BUG_FAILURE_REASONS` precedent for reusing that CHECK constraint in
> Python.
>
> **`aqrl review show` renders exactly the evidence A4 judged on.**
> `assemble_promote_brief` (`agents/context.py`) was split into a public
> `promotion_evidence_sections()` plus the prompt template, so there is one
> renderer, not two that could drift.
>
> **Deployment fields are derived, not invented.** `expected_sharpe`/
> `expected_max_dd`/`expected_win_rate`/`expected_avg_trade` are copied from
> the winning evaluation; `trades_required` from the resolved timeframe
> profile's `min_trades`; `regimes_required` from `eval.regimes.REGIMES`
> minus `crisis` — the four PRD §9.3 actually names.
>
> **Known limits, stated rather than papered over.** "Request more research"
> (a human defer) is not implemented — `promotions.human_decision`'s CHECK
> allows only `approved|rejected|pending`, so it needs a migration; A4's own
> `defer` path already shows the mechanism (`handlers/promote.py`).
> Portfolio correlation and the four UI-UX §3.2 charts are Stage 12's
> concern, needing live deployments and a dashboard respectively, neither of
> which exist yet. Gate 2 (paper→live) is wired via `promotions.stage_to ==
> 'live_small'` but has no real producer until Stage 11 — untested against
> anything but Gate 1's actual shape.
>
> **Pre-existing environment gap, not introduced here.** `vcs.py` imports
> `fcntl`, unavailable on native Windows — `tests/orchestration/*` and
> `tests/test_vcs.py` already failed to *collect* on that platform before
> this stage. `test_human_gates.py` inherits it for the same reason
> `test_promote_handler.py` does; every non-git code path in `gates.py`
> (`pending`, `evidence`, `reject`, and `approve`'s guard clauses) was
> smoke-tested directly against a real migrated database on Windows as a
> substitute, and the full suite needs a WSL/Linux run to close out.

---

## 13. Stage 10 — The Librarian & Curiosity Engine

**Goal:** stop the loop from feeding on itself (PRD §7.1), via the agent formalised in PRD §6.4 / TRD §12.2 — kept **outside** the five-agent research loop.

| Deliverable | Notes |
|---|---|
| Collectors (pure Python) | arXiv, SSRN, GitHub, blogs, market stats. **Claude never crawls.** One unified pipeline — GitHub sources contribute text, no separate code-graph tooling |
| Dedup + relevance filter | Cheap filtering **before** spending LLM tokens |
| Chunker | Splits large documents **by structure** (section/heading), not a blind token window |
| Per-chunk extraction | Pass 1 → `document_chunks.chunk_extraction` |
| Cross-chunk synthesis | Pass 2 → a few **distinct** ideas; one `external_knowledge` row per idea, never per document |
| Trust tagging | `evidence_tier = external_claim`; `extraction_confidence` measures reading accuracy, never truth |
| Curiosity queue | Failures → research questions → targeted collector searches |
| Loop closure tracking | `produced_spec_ids` — did asking ever pay off? |

**Done when:** a failure pattern automatically produces a targeted literature search whose results measurably influence a subsequent hypothesis, **and** a sample of extracted papers shows correctly-separated distinct ideas rather than one blob per document.

---

## 14. Stage 11 — Paper Trading & Health Monitoring

**Goal:** forward evidence, and the lifecycle answer.

| Deliverable | Notes |
|---|---|
| Paper trading executor | Per-market; internal simulation vs broker API is an open question |
| Trade recording | Including **expected vs actual slippage** |
| Daily health check job | Z-scores vs validated baseline, loss-distribution test, regime context, execution quality |
| Green/Yellow/Orange/Red logic | PRD §9.4 |
| **Regime-aware verdicts** | A drawdown in a historically weak regime is *expected*, not evidence of death |
| Promotion gate | Trade count + deviation + regime coverage + execution + health — **all** required |
| Hard risk limits + kill switch | Enforced outside strategy logic; makes a 100% drawdown structurally unreachable |
| Lifecycle events | Full audit trail, including deploy-branch removal on retirement |

**Done when:** a paper-traded strategy is monitored automatically and correctly distinguishes *"normal losing period"* from *"behaviour has changed."*

---

## 15. Stage 12 — Dashboard (decision layer)

Built to `UI-UX-Brief.md`. Order: Decisions → Health → Pipeline → Laboratory → Knowledge.

**Observability is not part of this stage** — it shipped at Stage 4a. Stage 12 is the *decision* layer only: a **read-only view plus two decision buttons**. By this point the human has been watching agents work and browsing the database for several stages; this adds only what is needed to approve or reject.

---

## 16. Stage 13 — Scale-Out

Only after the loop is proven and producing candidates.

| Step | Change |
|---|---|
| Parallel workers | Multiple A2/evaluate workers pulling the same queue |
| Redis | Job queue backend swap — agent code unchanged |
| Docker | Per-agent isolation |
| PostgreSQL | Metadata backend swap |
| Multi-machine | Shared storage, distributed workers |
| Kubernetes | Only when machine count justifies it |

**Constraint:** each step must be a **backend swap, not a rewrite.** If any step requires changing agent logic, Stage 1 or 4 got the abstraction wrong.

---

## 17. Milestones

| # | Milestone | Proves |
|---|---|---|
| **M0** | **Null-world FDR measured and low** | The pipeline does not invent discoveries. **Nothing downstream means anything without this** |
| **M1** | `evaluate.py` passes all known-answer tests | We can trust our own verdicts |
| **M2** | A2↔A3 loop iterates autonomously and stops correctly on a bar clear | The research loop closes |
| **M3** | A1 generates specs that feed the loop end to end | Full autonomy from goal to result |
| **M4** | Repeat-failure rate trends to zero | The lab has memory |
| **M5** | A strategy reaches human review with credible evidence | The funnel produces output |
| **M6** | **AQRL discovers one statistically valid strategy no human designed** | ★ The v1 KPI (PRD §4.1) |
| **M7** | A discovered strategy survives paper trading | Forward evidence, not just backtests |
| **M8** | Health monitoring correctly calls a live strategy's decline | The lifecycle closes |

**M0, M2 and M6 are the ones that matter.** M0 gates everything — a discovery from an uncalibrated pipeline is not a discovery. M6 is the whole point.

---

## 18. Risk Register

| Risk | Severity | Mitigation |
|---|---|---|
| **`evaluate.py` is subtly wrong** | 🔴 Critical | Known-answer suite **and** null-world calibration (M0) before trusting any result. Everything downstream inherits its errors |
| **Reward hacking — the agent optimises the scorer, not the market** | 🔴 Critical | `evaluate.py` unreadable and unwritable; `data.py` read-only; too-good-to-be-true tripwire; periodic red-teaming (TRD §15.1) |
| **Best-of-N selection bias** | 🔴 Critical | Satisficing against a pre-set bar + immediate stop on clearing it + the vault. "Best after 500 tries" is the luckiest, not the best |
| **Look-ahead bias in generated code** | 🔴 Critical | P0 static checks as a hard gate; failures classified as bugs, not findings |
| **Vectorisation introduces look-ahead** | 🔴 Critical | The fastest code leaks most easily. P0 plus a mandatory leaky-vectorised known-answer test (TRD §9.4) |
| **Trial under-counting → false discoveries** | 🔴 Critical | Family-level trial counting is a schema requirement. The same idea in 3 markets is 3 trials |
| **Building the full architecture before the simple loop runs** | 🟠 High | Stage 0 exists precisely to prevent this. Every later stage needs a felt pain |
| **The loop feeds on itself and plateaus** | 🟠 High | Stage 10 external ingestion; monitor novelty of generated specs |
| **A5's knowledge is never actually used** | 🟠 High | Repeat-failure rate is an explicit, tracked metric |
| **Parallelism breaks determinism** | 🟠 High | Per-fold seeds, fixed reduction order, chronological concatenation. Bit-identical re-run is a gate |
| **`evaluate.py` too slow to be useful** | 🟠 High | Seconds-not-minutes target; per-experiment time budget; profile before optimising |
| **Token cost outruns value** | 🟠 High | Budgets with hard back-pressure; cost-per-discovery on the Laboratory screen |
| **Human rubber-stamps approvals** | 🟠 High | Case-against-first UI; mandatory typed justification |
| **Scaling requires a rewrite** | 🟡 Medium | No SQLite-specific SQL; lease-based queue from day one |
| **NIFTY-50 survivorship bias** ⚠️ | 🔴 Critical — **mitigation chosen, blocked on data** | Fix is point-in-time index membership (TRD §15.3). Needs membership history *and* price data for all ~100–150 ever-members. **Indian equity results must not reach live capital until both exist.** Index-level research is structurally unaffected and should run in the meantime |
| **Unadjusted source data** ⚠️ | 🔴 Critical | Splits and bonuses appear as phantom ±50% moves, corrupting every price-based indicator. Mitigated by the Stage 1 adjustment pipeline (TRD §15.2) — but that pipeline is only as complete as the corporate-actions data behind it, hence the unexplained-jump validator as a second line of defence |
| **Incomplete corporate-actions history** | 🟠 High | A missing split silently corrupts one instrument's entire series. The jump validator catches large ones; small bonuses may slip through. Prefer vendor-adjusted data if obtainable |
| **Cost model sourced from public rates, not contract notes** | 🟠 High | ~22 bps statutory on NSE delivery is a *starting default*. Re-derive from a real broker contract note before live capital — stale rates are a silent systematic bias in every backtest |
| **Sub-minute backtest fidelity** | 🟠 High | Below ~1 minute, fills depend on queue position and latency that bar data cannot represent. Supported ≠ trustworthy (TRD §6.4) |
| **Data quality corrupts everything silently** | 🟡 Medium | Validators at ingest; immutable hashed snapshots |

---

## 19. The Prior Prototype

> **The working tree contains only these seven documents.** An earlier prototype — Drive notebook ingestion, indicator extraction, a backtest engine, tradebook generation — was removed to give the build a clean start.
>
> **Nothing is lost — it lives in git history at commit `d08e812`:**
> ```bash
> git show d08e812:engine/backtester.py     # view a file
> git checkout d08e812 -- engine/           # restore a directory
> git checkout d08e812 -- .gitignore        # wanted back before the first run
> ```

| In `d08e812` | Informs |
|---|---|
| `engine/backtester.py`, `tradebook.py` | Backtest core inside `evaluate.py` (Stages 0, 3) |
| `engine/jobs.py`, `registry.py` | Job and discovery patterns (Stages 3–4) |
| `utils/indicator_registry.py` | Operator library seed (Stage 2) |
| `ai/strategy_generator.py` | A1 seed (Stage 7) |
| `ai/indicator_extractor.py`, `summarizer.py` | Librarian extraction (Stage 10) |
| `ai/llm_client.py` | LLM plumbing, key rotation (Stages 5–8) |
| `utils/chunking.py` | The chunker's starting point (Stage 10) |
| `drive/` | An additional collector source (Stage 10) |
| `config.py`, `schemas.py` | Config and validation foundations (Stage 1) |
| Existing JMA+ATR, walk-forward, Monte Carlo work | Operators + robustness battery (Stages 2–3) |

These are **reference implementations to borrow from, not a codebase to extend.** Stage 0 is written fresh against the five-file structure.

---

## 20. Explicitly Deferred

Not in the v1 build:

- Multi-strategy portfolio construction and correlation-aware allocation (PRD §3)
- Live broker integration — paper trading only until M7
- Options and derivatives
- Distributed / cloud anything
- The decision dashboard, until Stage 12
- Self-modifying operator library — the human review gate stays
- **Any automatic path to live capital**

---

## 21. Open Planning Questions

- [ ] The numeric acceptance bar and `z` multiplier for the first campaign, written before searching. *Owner: human*
- [ ] Which train window (1/2/3 years) for the first campaign. *Owner: human*
- [ ] Vault composition and per-family peek budget. *Owner: human*
- [ ] Which market/timeframe is the first fully-supported profile? (Leaning `nse_equity` × `daily`)
- [ ] Do we port existing JMA+ATR work into the operator library, or rewrite clean against the new base class?
- [ ] Known-answer test corpus — which published strategies serve as ground truth? *Stage 3 covers the mandatory look-ahead/leakage/noise/regime/cost/determinism cases via analytic ground truth instead (§5.2); a real published-strategy comparison is still open, blocked on the same real price history as everything else.*
- [x] Does Stage 3 ship all six markets' profiles, or one market first and the rest after M2? — **One market first.** `nse_equity` × `daily` ships; the crypto/commodities/forex gates are implemented generically against TRD §6.5's "extra gates, not extra engines" and exercised against synthetic profile variants, so a real profile is a YAML file away rather than new engine code.
- [ ] Paper trading: internal simulator vs broker paper API, per market?

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial plan — 13 stages, validation-engine-first ordering, A2/A3 before A1, milestones, risk register, reuse mapping. |
| 2026-07-27 | Added Stage 0 (nanoAQRL) as the real starting point and M0 (null-world FDR) as the gating milestone. Added Stage 4a (Observability), pulled out of the Stage 12 dashboard. |
| 2026-07-28 | Stage 6 gained the bar-clear short-circuit; Stage 8's A4 lost portfolio correlation; Stages 5 and 9 gained the git branch and merge-on-approve steps. |
| 2026-07-28 | **Design decisions locked in** — Stage 0.1 specifies best-of-three train windows with the ×3 trial count and per-fold tuning; Stage 1 gained per-`(market, asset_class)` cost models and the 1s–1month profile range. |
| 2026-07-28 | **Data integrity added to Stage 1** — the corporate-action adjustment pipeline, the unexplained-jump validator, and point-in-time universe resolution. Added the survivorship, unadjusted-data and incomplete-actions risks. |
| 2026-07-28 | **Full rewrite.** Cross-references updated to the renumbered TRD; changelog consolidated. No plan decisions changed. |
| 2026-07-28 | **Stage 0 built.** nanoAQRL's five files, the honest score, best-of-three walk-forward, the vault, P0 look-ahead scans, and null-world calibration — **FDR measured at 0/40 on all three null models, clearing M0.** |
| 2026-07-28 | **Stage 1 built.** The `aqrl/` package: config, content hashing, correlation-ID logging, migrations covering every `Backend-Schema.md` table, a thin repository layer, content-hashed profiles with **derived** annualisation across 1s–1month, and the data layer (snapshots, load-time adjustment, point-in-time universe, ingest validators) plus the `aqrl` CLI. `nanoaqrl` ported onto the canonical schema; its hardcoded `PERIODS_PER_YEAR = 252` and duplicate cost model are gone. Point-in-time membership remains blocked on data collection. |
| 2026-07-29 | **Stage 1's data layer restored** (PR #3). A bare `data/` pattern in `.gitignore` matches a directory of that name at *any* depth, so it silently excluded the entire `aqrl/data/` package from the Stage 1 commit — the CLI's snapshot commands crashed and four test modules failed at import on a clean checkout, while every working tree that had the files locally passed. Pattern anchored to `/data/`. Worth remembering as a class of bug: the tooling reported success because the artefact under test was never the artefact committed. |
| 2026-07-29 | **Stage 2 built.** `aqrl/operators/`: the base class and registry, **32 operators** across all four TRD §11 categories, the spec DAG with canonical structural hashing, and a compiler producing the signal function `nanoaqrl/evaluate.py` already accepts — verified bar-for-bar identical to the hand-written `strategy.py`. Node ids, declaration order, defaults, float spelling, commutative operand order and hypothesis wording all leave `spec_hash` unchanged; a genuine change does not. Causality is a registry-wide property test with negative controls, and `operator_library_version` is derived by content hash. No new dependencies. |
| 2026-07-29 | **Stage 3 built.** `aqrl/eval/` — the single, profile-driven, panel-native evaluation engine: the ordered funnel (complexity → P0 → P1 → P2 → P3 → market gates → the bar), the walk-forward protocol and honest score moved out of `nanoaqrl/_lib/` so exactly one implementation exists (TRD §6.1), market-specific gates as appended phases per TRD §6.5, and full TRD §6.6 provenance persisted into Stage 1's schema with no new migration needed. The known-answer suite (§5.2) passed — **M1 cleared** — with the published-strategy case replaced by analytic ground truth. Profiling drove two evidence-based optimisations (Numba on the one genuinely path-dependent hot loop; an O(n²) expanding-median bug fixed algorithmically) for a measured 4.7× speedup, and fold-level parallelism was verified bit-identical across worker counts, not merely designed to be. One new dependency: `numba`, applied on profiled evidence per TRD §9.7, not speculatively. |
| 2026-07-29 | **Stage 4 built.** `aqrl/orchestration/` — the `jobs` queue (`JobRepository`: atomic `BEGIN IMMEDIATE` claim, lease/heartbeat, `dedupe_key`), explicit state machines for `strategies`/`experiments` that raise on an invalid transition and write `audit_log`, the TRD §4.1 event→job_type table, transient/deterministic failure classification with exponential backoff and *k*-consecutive-failure quarantine, budget back-pressure gating dispatch, a subprocess worker with the `EVALUATE` handler wired end to end (queued job → rebuilt `EvaluationInputs` → Stage 3's engine → persisted report → the bar-clear short-circuit straight to `PROMOTE`, or `REVIEW` on a bar failure — never invoking A3, which doesn't exist yet), and the App-Flow §13 scheduler tick with TRD §4.5 idle-cause reporting. `aqrl.db.connection` gained WAL mode, `busy_timeout`, and `BEGIN IMMEDIATE` for the first time two processes write the database at once. Migration 0008 added `jobs.dedupe_key`. The crash test *is* the done-when: a real subprocess is `SIGKILL`ed mid-job and a re-run produces exactly one evaluation, and losing the scheduler process itself still recovers via lease expiry on the next tick — both proven against real OS processes, not mocks. |
| 2026-07-30 | **Stage 5 built.** `aqrl/agents/` (render, sandbox, session, context) and `aqrl/orchestration/handlers/implement.py` — A2 translates a spec (hand-written for iteration 1, Claude-proposed for a plan-driven iteration or a `FIX_CODE` retry) into `strategies/<uid>/strategy.py` via a deterministic renderer, never freeform code, since `evaluate.py` compiles specs (TRD §6.1's no-forking rule extended to codegen). Static checks run in a scrubbed-env, resource-limited, timed-out subprocess reusing Stage 3's own P0 scanners; a passing spec commits to an idempotent, orphan-per-strategy git branch (`aqrl/vcs.py`) and enqueues `EVALUATE` unattended, exactly as Stage 5's done-when requires — proven both via direct handler calls and through a real claimed job in a real worker subprocess. A failing spec is a bounded `FIX_CODE` retry loop (default 3 attempts, counted from `code_versions`) ending in quarantine, not a failed job. No new migration: `code_versions` and `research_plans` already existed in Stage 1's schema, needing only new repositories (`CodeVersionRepository`, `ResearchPlanRepository`) and `ExperimentRepository.open_pending` for the `created → code_pending → code_ready → evaluating` chain Stage 4 defined but never drove. New optional dependency: `anthropic`, imported lazily so no test in the suite needs a network connection or an API key — `StubSession`/`ReplaySession` stand in throughout. **Review caught one real bug before merge:** `aqrl/vcs.py`'s shared working tree had no cross-process locking, so `Dispatcher`'s default `max_concurrent=4` (Stage 4) could run two strategies' `IMPLEMENT` jobs in parallel subprocesses racing `git checkout`/`init`/`commit` against the one shared repo — reproduced directly (four concurrent processes, three ended up crashed or missing their file entirely). Fixed with an exclusive `fcntl.flock` held across each public method's full git sequence, with a regression test exercising real concurrent subprocesses. |
| 2026-08-04 | **Stage 8 built.** `aqrl/orchestration/handlers/promote.py` (A4) and `handlers/archive.py` (A5, serving both `ARCHIVE` and `MINE_PATTERNS`) close the two dead ends the loop had run into since Stage 6/7: a bar-clearing evaluation's `PROMOTE` job and a plateaued/rejected strategy's `ARCHIVE` job both previously hit `NotImplementedHandler`. New repositories (`PromotionRepository`, `KnowledgeEdgeRepository`, `LabNotebookRepository`, write paths on `KnowledgeEntryRepository`/`ResearchQuestionRepository`) and two new `Proposed*` session shapes (`ProposedPromotion`, `ProposedKnowledge`) follow every existing convention — no migration needed, since Stages 1/3 already created every table Stage 8 writes. A4's brief is the one deliberate exception to the system's usual redaction: it may see `honest_score` and full metrics, since A4 only ever emits a human-confirmed recommendation, never a spec-shaping signal; A5's brief keeps A3's asymmetric redaction instead, because A5's lessons **do** reach a future A1 brief. `states.py` gained `pending_promotion → rejected` (a deliberate, documented deviation from Backend-Schema §14.1, following the precedent Stages 3/5/6 already set for gaps this specific), and `scheduler.TIME_DRIVEN_SCHEDULE` got its first live entry (`MINE_PATTERNS`, weekly) — the mechanism Stage 4 built and deliberately left empty until there was a real producer. `db.repositories.knowledge.repeat_failure_rate` operationalises Implementation_Plan §11's done-when as a structured-field comparison (`knowledge_entries.evidence.failure_reasons`, populated by `handlers/archive.py`) rather than free-text matching, exposed via `aqrl knowledge rate`. Proven both directly (`tests/test_knowledge_repositories.py`, `test_promote_handler.py`, `test_archive_handler.py`) and end to end through the real job queue (`test_memory_loop.py`): a rejected experiment's lesson is visible through the exact reader a future A1 brief calls, and a second, later strategy failing the identical way is correctly counted as a preventable repeat. **One regression caught and fixed on the way:** populating `TIME_DRIVEN_SCHEDULE` for the first time broke three Stage 4/6 tests that had assumed it was permanently empty (two asserted `tick()` dispatched nothing; one used `ARCHIVE` as its example of "a job type nobody services yet," which stopped being true) — fixed by isolating the dispatch-mechanics tests with an explicit `schedule=[]` and swapping that fixture to `COLLECT_PAPERS`, still genuinely unimplemented. |
| 2026-08-05 | **Stage 9 built.** `aqrl/gates.py` (`pending`/`evidence`/`approve`/`reject`) closes the loop's last dead end: A4's `promotions` row previously sat at `human_decision='pending'` forever with nothing reading it back. `approve` is the only code path in the system that merges `strategy/<uid>` into `deploy/paper`/`deploy/live` (`StrategyRepo.merge`, new in `vcs.py`), opens the vault (`VaultAccessRepository`, budget derived from `vault_access_log`'s own row count, no new counter table), and inserts a `deployments` row (`DeploymentRepository`, `LifecycleEventRepository` — both new, same migration as the existing `PromotionRepository`) — TRD §18's "no code path may bypass these gates," made true rather than merely stated. `aqrl review list/show/approve/reject` in `cli.py` is a thin shell over it. No migration: every column already existed. Scoped down from the full spec on purpose: vault access is gate-only (logs and decrements the family budget; does not load the vault snapshot or score against it — App-Flow §15's full flow needs a vault snapshot that does not exist yet), and a human rejection reaches A5's knowledge base by a direct, human-authored `knowledge_entries` write rather than re-running `ARCHIVE` (its `lab_notebooks` idempotency guard makes a second run for the same strategy a no-op by design, TRD §12.1). `agents/context.py`'s `assemble_promote_brief` was split to expose `promotion_evidence_sections()` publicly, so `aqrl review show` renders exactly the evidence A4 judged on with no second renderer to drift. Proven directly against a real migrated database (`tests/orchestration/test_human_gates.py`, `tests/test_vcs.py`'s new merge cases) — including a human rejection counted by Stage 8's `repeat_failure_rate` exactly like an A5-recorded one. **One real bug caught before merge:** the deferred `vcs` import (kept out of `gates.py`'s module scope so `pending`/`evidence`/`reject` stay usable on a platform without `fcntl`) was originally placed at the top of `approve`, ahead of its own validation guards — an empty note or an already-decided promotion failed on an unrelated import instead of its own clear error. Moved to sit right before its one use, verified by exercising every guard clause directly. |
