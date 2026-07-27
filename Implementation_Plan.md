# Implementation Plan — AQRL

> **Status:** Living document. Updated after every design session.
> **Last updated:** 2026-07-27
> **Nothing here is built yet.** This is the build order for the one-shot implementation.

---

## 0. Build Philosophy

**Build the funnel before the factory.** The bottleneck in quant research is not generating ideas — it is reliably distinguishing genuine alpha from overfitting. So the validation engine is built first, and the idea generator is built last. A lab that generates 10,000 hypotheses with a weak evaluator is worse than useless: it manufactures false confidence at scale.

**Every stage must produce standalone value.** No stage exists purely as scaffolding for the next one. If we stop after Stage 3, we still have a rigorous evaluation harness worth using by hand.

**Prove the loop closes before making it fast.** Single-threaded, one laptop, SQLite. Parallelism and cloud are Stage 9+, and only after the loop demonstrably works.

**Start smaller than feels right.** The reference project (PRD §14) runs a complete autonomous research loop in three files. Stage 0 mirrors that. Every stage after it must be triggered by a **pain actually felt** — the loop plateaus, a question arrives that `results.tsv` cannot answer, one agent visibly fails at one job — never by the plan alone. The six documents describe a good destination and a bad starting point.

---

## 1. Dependency Graph

```
Stage 0  nanoAQRL + integrity   ★ START HERE
    │        (honest score · evaluate.py · null-world · vault · 5 files)
    │
Stage 1  Foundations (config, DB, profiles, data layer)
    │
    ├──► Stage 2  Operator Library
    │        │
    ├──► Stage 3  evaluate.py  ◄──┘        ← THE CRITICAL PATH
    │        │
    │        ▼
    ├──► Stage 4  Job queue + scheduler
    │        │
    │        ▼
    │    Stage 5  A2 Quant Engineer  ──┐
    │        │                         │  the inner loop
    │        ▼                         │
    │    Stage 6  A3 Research Reviewer ┘
    │        │
    │        ▼
    │    Stage 7  A1 Research Scientist
    │        │
    │        ▼
    │    Stage 8  A4 Promotion + A5 Knowledge
    │        │
    │        ▼
    │    Stage 9  Human gates (CLI, then dashboard)
    │        │
    │        ▼
    │    Stage 10 External ingestion + curiosity engine
    │        │
    │        ▼
    └──► Stage 11 Paper trading + health monitoring
             │
             ▼
         Stage 12 Dashboard UI
             │
             ▼
         Stage 13 Scale-out
```

**Note the ordering choice:** A2 and A3 (Stages 5–6) come *before* A1 (Stage 7). The iteration loop is the heart of the system and can be exercised with hand-written specs. Building A1 first would mean generating hypotheses with nothing capable of properly testing them.

---

## 1A. Stage 0 — nanoAQRL ★ START HERE

**Goal:** a complete autonomous research loop, in five files, whose verdicts are proven trustworthy before a single real-data result is believed.

Stages 1–13 remain the destination. None of them begin until Stage 0 has run for real.

### 1A.1 The work, in strict order

| # | Task | Why this order |
|---|---|---|
| **0.1** | **Decide the honest score** (TRD §4A) | A thinking task, not a coding task. Nothing works until this exists — a bigger loop on a dishonest score just produces wrong answers faster |
| **0.2** | **Build `evaluate.py`** around that score | Structurally isolated: the agent can neither read nor edit it |
| **0.3** | **Run the null-world test** (TRD §8A.3) | Prove the scorer does not invent discoveries in pure noise. Fix and re-run until FDR is low |
| **0.4** | **Build the vault** (TRD §8A.2) | Lock the holdout *before* the loop ever touches real data |
| **0.5** | Write `program.md` and `strategy.py`, wire the keep/reset loop | Small, once 0.1–0.4 exist |
| **0.6** | **Run it one night. Read every row of `results.tsv` by hand** | The only way to learn what the agent actually does |
| **0.7** | Improve `program.md` from what you saw | Repeat for several weeks |

**Steps 0.1–0.4 are the real work. 0.5 is small. That ratio is the point.**

### 1A.2 Deliverables

```
data.py        # snapshots, calendars, costs, universe   — agent: READ ONLY
strategy.py    # the one file the agent edits
evaluate.py    # the harness                             — agent: NO READ, NO WRITE
program.md     # instructions + acceptance bar           — human-edited only
results.tsv    # commit | score | n_trades | status | description
```

Plus three SQLite tables (`strategies`, `experiments`, `evaluations`) with `experiments.code_commit` pointing at git. No job queue, no scheduler, no agents beyond the one.

### 1A.3 Done when

- Null-world FDR measured and low — **the pipeline does not manufacture discoveries**
- The vault exists and the loop provably cannot read it
- The loop runs unattended overnight and every row carries a verdict
- The acceptance bar was written down *before* the search started and was not moved afterwards

---

## 2. Stage 1 — Foundations

**Goal:** the substrate everything else assumes.

| Deliverable | Notes |
|---|---|
| Project skeleton | `aqrl/` package alongside existing code; do not disturb current `main.py` pipeline |
| Config system | Env + file, no secrets in code |
| SQLite schema | Full `Backend-Schema.md`, with migrations from day one |
| DB access layer | Thin repository pattern; **no SQLite-specific SQL** (TRD §13) |
| `MarketProfile` / `TimeframeProfile` loaders | YAML → validated object → content hash |
| First profiles | `nse_equity` × `{daily, 15min}` — the market we know best |
| Data snapshot manager | Ingest CSV/Parquet → immutable versioned snapshot + content hash |
| Per-market data validators | Run at ingest, not at experiment time |
| Structured logging | Correlation IDs threading strategy → experiment → job |

**Done when:** a market snapshot can be ingested, versioned, hashed, and loaded by ID; profiles resolve and hash deterministically.

**Reuses:** `config.py`, `scan_market_data.py`, `utils/file_utils.py`.

---

## 3. Stage 2 — Operator Library

**Goal:** the vetted vocabulary strategies are composed from (TRD §6).

| Deliverable | Notes |
|---|---|
| Operator base class + registry | Declares category, parameters with valid ranges, valid markets/timeframes |
| Transformations | Rolling mean, EMA, **JMA**, Kalman, ATR normalization, fractional differencing, PCA, wavelets |
| Signal operators | Crossover, threshold, breakout, volatility expansion, momentum, mean reversion, volume confirmation |
| Risk operators | ATR stop, time stop, trailing stop, position sizing, Kelly variants, volatility targeting |
| Portfolio operators | Equal weight, risk parity, correlation clustering |
| Unit tests per operator | Non-negotiable. An untested operator cannot enter the library |
| Spec DAG format + canonical hashing | Enables duplicate detection (`spec_hash`) |
| Operator seeding from `indicator_library.json` | Existing extracted indicators become candidates, gated by review |

**Done when:** a strategy spec can be expressed purely as an operator composition, hashed canonically, and two logically identical specs produce the same hash.

**Reuses:** `utils/indicator_registry.py`, existing JMA/ATR work.

---

## 4. Stage 3 — `evaluate.py` ★ CRITICAL PATH

**Goal:** the single, profile-driven evaluation engine (TRD §4). This is the most important code in the system.

### 4.1 Sub-stages

**3a — Backtest core**
- Profile-driven cost model, fill model, calendar
- Correct annualization from `TimeframeProfile.periods_per_year` (a hardcoded constant here is a project-level bug)
- Tradebook + equity curve → Parquet

**3b — Phase 0 correctness checks** *(build before any metric)*
- Look-ahead detection (shift/lag verification)
- Data-leakage scan
- Survivorship / point-in-time checks
- Signal→order→fill ordering validation
- NaN/inf guards, trade-count guard

> Rationale: look-ahead bias is the dominant failure mode of LLM-written strategy code. A P0 failure is a **bug**, routed to A2 — it must never be recorded as a research finding.

**3c — Core metrics (P1, P2)**
- Sharpe, Sortino, Calmar, CAGR, max/avg DD, DD duration, profit factor, win rate, expectancy, turnover, exposure

**3d — Robustness battery (P3)**
- Walk-forward (bar-sized **and** calendar-constrained)
- Monte Carlo (variant TBD — see TRD open questions)
- **Deflated Sharpe with correct trial counting** — queries the DB for family trial count
- White's Reality Check
- CSCV / PBO
- Regime analysis
- Cost sensitivity sweep (multipliers from the timeframe profile)
- Parameter sensitivity

**3e — Market-specific gates** (TRD §4.6)
- Crypto: venue robustness, funding sensitivity
- Equities: survivorship, capacity/ADV
- Commodities: roll-method sensitivity
- Forex: session dependence

**3f — Provenance & reporting**
- Stamp `eval_engine_version`, both profile hashes, snapshot ID, operator library version, seed
- Structured JSON report + human-readable markdown

### 4.2 Validation of the validator

Before trusting it, `evaluate.py` must be tested against **known-answer cases**:

- A deliberately look-ahead-biased strategy → P0 must catch it
- A pure-noise random strategy → must not pass P3
- A strategy overfit to one regime → PBO must flag it
- A strategy with a real but small edge → must survive P2, die on realistic costs
- A known-good published strategy → results within tolerance of published figures

**Done when:** all known-answer cases behave correctly, and identical inputs reproduce identical outputs bit-for-bit.

**Reuses:** `engine/backtester.py`, `engine/tradebook.py`, existing walk-forward/Monte Carlo/robustness work.

---

## 5. Stage 4 — Nervous System

**Goal:** the coordination layer (TRD §3).

| Deliverable | Notes |
|---|---|
| `jobs` table + queue API | Atomic claim with lease + heartbeat |
| `scheduler.py` | Single always-on tick loop, ~60s |
| Worker dispatch | Subprocess, isolated, reaped with cost accounting |
| State machine | Enforces valid transitions; invalid transitions are errors, not warnings |
| Event → job mapping | The table in TRD §3.1 |
| Budget enforcement | Tokens, iterations, experiments, compute — with back-pressure |
| Failure classification | Transient (retry) vs deterministic (route to fix) |
| Quarantine logic | Poison-pill protection after *k* consecutive failures |
| Crash recovery | Lease expiry returns orphaned jobs; `kill -9` loses nothing |

**Done when:** the scheduler survives `kill -9` mid-job with zero state loss and zero duplicated work.

---

## 6. Stage 5 — A2 Quant Engineer

**Goal:** spec → working code.

| Deliverable | Notes |
|---|---|
| Claude session wrapper | Stateless, structured output, schema-validated |
| Context assembler | **Python builds the brief** — spec, research plan, prior code + diff, prior evaluation, operator catalog |
| Code generation | Emits a strategy module composed from operators |
| Sandboxed execution | No network, no credentials (TRD §11) |
| Static check pipeline | Compile, lint, look-ahead scan before evaluation is even queued |
| `FIX_CODE` path | Deterministic failures return with diagnostics attached |
| Prompt versioning | Recorded on every output |

**Done when:** given a hand-written spec, A2 produces code that passes P0 and runs through `evaluate.py` unattended.

---

## 7. Stage 6 — A3 Research Reviewer ★ CLOSES THE LOOP

**Goal:** the iteration engine — the first moment AQRL is more than a pipeline.

| Deliverable | Notes |
|---|---|
| Context assembler | Spec, **full** iteration history, evaluation report, per-test results, regime breakdown, related knowledge |
| Verdict logic | `iterate` / `plateau` / `promote` / `reject` |
| Research plan output | **Plain-language changes, never code** (PRD §6.1) |
| Stop-condition evaluation | Checked in Python *before* invoking Claude, so budget is never wasted |
| Plateau detection | Tracks improvement across iterations |
| Loop wiring | A3 → A2 → evaluate → A3 |

**Done when:** a hand-written spec runs autonomously through 10+ iterations, improves measurably, and stops for a principled reason rather than a hard cap.

> **This is the first real milestone.** At this point the system iterates on research without a human. Everything before it is infrastructure; everything after it is amplification.

---

## 8. Stage 7 — A1 Research Scientist

**Goal:** autonomous hypothesis generation.

| Deliverable | Notes |
|---|---|
| Context assembler | Goals + allocation bucket, knowledge entries, graph edges, open questions, recent failures, operator catalog |
| Spec generation | Operator composition + parameter ranges + rationale + `expected_behavior` |
| Duplicate rejection | `spec_hash` check before any compute is spent |
| **Anti-amnesia injection** | Surfaces contradicting lessons; A1 must justify overriding them |
| Portfolio allocation | Honors the 70/20/10 split (PRD §4.4) |
| Batch generation | Nightly, budget-bounded |

**Done when:** A1 generates novel, non-duplicate specs that respect known failure patterns, and the full A1→A2→evaluate→A3 loop runs end to end unattended.

**Reuses:** `ai/strategy_generator.py`, `ai/llm_client.py`.

---

## 9. Stage 8 — A4 Promotion + A5 Knowledge

**Goal:** close the learning loop.

### A4 — Promotion Committee
- Context = the **entire** research history, not just the winner
- Explicit weighing of iteration count as an overfitting signal
- Correlation check against live portfolio
- Outputs a recommendation record; **no credentials, no execution authority**

### A5 — Knowledge Manager
- Per-experiment: lab notebook with **mandatory non-empty next-questions**
- Knowledge entries with evidence **and counter-evidence** counts
- Knowledge graph edges (subject–predicate–object), every edge backed by experiments
- Weekly cross-experiment pattern mining → global rules
- Research questions pushed to the curiosity queue

**Done when:** rejected experiments demonstrably prevent similar future proposals — measured by the **repeat-failure rate** trending toward zero.

> This is the second real milestone: the lab stops being a loop and starts being a **memory**.

---

## 10. Stage 9 — Human Gates (CLI first)

**Goal:** the safety boundary, before any UI work.

| Deliverable | Notes |
|---|---|
| `aqrl review` CLI | Lists pending decisions, renders the full evidence package |
| Approve/reject with mandatory note | Recorded to `promotions` |
| Structured rejection reasons | Flow into A5 as knowledge |
| Deployment record creation | Baseline `expected_*` copied from validation |

**Done when:** a human can make a fully-informed gate decision from the terminal. The dashboard is deferred — a CLI is sufficient to validate the loop.

---

## 11. Stage 10 — External Knowledge & Curiosity

**Goal:** stop the loop from feeding on itself (PRD §7.1).

| Deliverable | Notes |
|---|---|
| Collectors (pure Python) | arXiv, SSRN, GitHub, blogs, market stats. **Claude never crawls** |
| Dedup + relevance filter | Cheap filtering before spending LLM tokens |
| `EXTRACT_KNOWLEDGE` job | LLM reads each document exactly once, ever |
| Structured external knowledge + embeddings | Store knowledge, not documents |
| Curiosity queue | Failures → research questions → targeted collector searches |
| Loop closure tracking | `produced_spec_ids` — did asking ever pay off? |

**Done when:** a failure pattern automatically produces a targeted literature search whose results measurably influence a subsequent hypothesis.

**Reuses:** `ai/indicator_extractor.py`, `ai/summarizer.py`, `utils/chunking.py`, `drive/` fetch patterns.

---

## 12. Stage 11 — Paper Trading & Health Monitoring

**Goal:** forward evidence and the lifecycle answer.

| Deliverable | Notes |
|---|---|
| Paper trading executor | Per-market; internal simulation vs broker API is an open question |
| Trade recording | Including **expected vs actual slippage** |
| Daily health check job | Z-scores vs validated baseline, loss-distribution test, regime context, execution quality |
| Green/Yellow/Orange/Red logic | PRD §9.4 |
| Regime-aware verdicts | A drawdown in a historically weak regime is *expected*, not evidence of death |
| Promotion gate | Trade count + deviation + regime coverage + execution + health, **all** required |
| Hard risk limits + kill switch | Enforced outside strategy logic; makes 100% drawdown structurally unreachable |
| Lifecycle events | Full audit trail |

**Done when:** a paper-traded strategy is monitored automatically and correctly distinguishes "normal losing period" from "behavior has changed."

---

## 13. Stage 12 — Dashboard

Built to `UI-UX-Brief.md`. Order: Decisions → Health → Laboratory → Knowledge → Pipeline → System.

Deliberately last. It is a **read-only view plus two decision buttons**; building it earlier would be decorating a lab that cannot yet run an experiment.

---

## 14. Stage 13 — Scale-Out

Only after the loop is proven and producing candidates.

| Step | Change |
|---|---|
| Parallel workers | Multiple A2/evaluate workers pulling the same queue |
| Redis | Job queue backend swap — agent code unchanged |
| Docker | Per-agent isolation |
| PostgreSQL | Metadata backend swap |
| Multi-machine | Shared storage, distributed workers |
| Kubernetes | Only when machine count justifies it |

**Constraint:** each step must be a **backend swap, not a rewrite.** If any step requires changing agent logic, Stage 1/4 got the abstraction wrong.

---

## 15. Milestones

| # | Milestone | Proves |
|---|---|---|
| **M0** | **Null-world FDR measured and low** | The pipeline does not invent discoveries. **Nothing downstream means anything without this** |
| **M1** | `evaluate.py` passes all known-answer tests | We can trust our own verdicts |
| **M2** | A2↔A3 loop iterates autonomously on a hand-written spec | The research loop closes |
| **M3** | A1 generates specs that feed the loop end to end | Full autonomy from goal to result |
| **M4** | Repeat-failure rate trends to zero | The lab has memory |
| **M5** | A strategy reaches human review with credible evidence | The funnel produces output |
| **M6** | **AQRL discovers one statistically valid strategy no human designed** | ★ The v1 KPI (PRD §4.1) |
| **M7** | A discovered strategy survives paper trading | Forward evidence, not just backtests |
| **M8** | Health monitoring correctly calls a live strategy's decline | The lifecycle closes |

M0, M2 and M6 are the ones that matter. **M0 gates everything** — a discovery from an uncalibrated pipeline is not a discovery. M6 is the whole point.

---

## 16. Risk Register

| Risk | Severity | Mitigation |
|---|---|---|
| **`evaluate.py` is subtly wrong** | 🔴 Critical | Known-answer test suite **and null-world calibration (M0)** before trusting any result. Everything downstream inherits its errors |
| **Reward hacking — the agent optimises the scorer, not the market** | 🔴 Critical | `evaluate.py` unreadable and unwritable; `data.py` read-only; too-good-to-be-true tripwire; periodic red-teaming (TRD §8A.1) |
| **Best-of-N selection bias** | 🔴 Critical | Satisficing against a pre-set bar (PRD §13.2) + the vault (TRD §8A.2). "Best after 500 tries" is the luckiest, not the best |
| **Building the full architecture before the simple loop runs** | 🟠 High | Stage 0 exists precisely to prevent this. Every later stage needs a felt pain to justify it |
| **Look-ahead bias in generated code** | 🔴 Critical | P0 static checks as a hard gate; failures classified as bugs, not findings |
| **Trial under-counting → false discoveries** | 🔴 Critical | Family-level trial counting is a schema requirement, not an afterthought |
| **The loop feeds on itself, plateaus** | 🟠 High | Stage 10 external ingestion; monitor novelty of generated specs |
| **A5's knowledge is never actually used** | 🟠 High | Repeat-failure rate is an explicit, tracked metric |
| **Token cost outruns value** | 🟠 High | Budgets with hard back-pressure; cost-per-discovery on the Laboratory screen |
| **Human rubber-stamps approvals** | 🟠 High | Case-against-first UI; mandatory typed justification |
| **Scaling requires a rewrite** | 🟡 Medium | No SQLite-specific SQL; lease-based queue from day one |
| **Data quality corrupts everything silently** | 🟡 Medium | Validators at ingest; immutable hashed snapshots |
| **Overbuilding before proving value** | 🟡 Medium | This staged plan; stop-and-assess after M2 and M6 |

---

## 17. What Gets Reused from the Prior Prototype

> **The working tree now contains only these six documents.** The prior prototype (Drive notebook ingestion, indicator extraction, backtest engine, tradebooks) was removed from the tree to give the build a clean start.
>
> **Nothing is lost — it lives in git history at commit `d08e812`.** Recover any file with:
> ```
> git show d08e812:engine/backtester.py
> git checkout d08e812 -- engine/          # restore a directory
> ```

| In `d08e812` | Becomes |
|---|---|
| `engine/backtester.py`, `tradebook.py` | Backtest core inside `evaluate.py` (Stage 3) |
| `engine/jobs.py`, `registry.py` | Job/discovery patterns (Stages 3–4) |
| `utils/indicator_registry.py` | Operator library seed (Stage 2) |
| `ai/strategy_generator.py` | A1 seed (Stage 7) |
| `ai/indicator_extractor.py`, `summarizer.py` | Knowledge extraction (Stage 10) |
| `ai/llm_client.py` | LLM plumbing, key rotation (Stages 5–8) |
| `utils/chunking.py` | Document processing (Stage 10) |
| `drive/` | An additional collector source (Stage 10) |
| `config.py`, `schemas.py` | Config and validation foundations (Stage 1) |
| Existing JMA+ATR, walk-forward, Monte Carlo, robustness work | Operators + robustness battery (Stages 2–3) |

These are **reference implementations to borrow from**, not a codebase to extend. Stage 0 is written fresh against the five-file structure; the prototype is consulted for the backtest mechanics, indicator maths and LLM plumbing already solved there.

---

## 18. Explicitly Deferred

Not in the one-shot build:

- Multi-strategy portfolio construction and allocation
- Live broker integration (paper trading only until M7)
- Options and derivatives
- Distributed/cloud anything
- The dashboard, until Stage 12
- Self-modifying operator library (human review gate stays)
- Any automatic path to live capital

---

## 19. Open Planning Questions

- [ ] **★ The honest score (TRD §4A) — Stage 0.1, blocks everything**
- [ ] The numeric acceptance bar for the first campaign, written before searching
- [ ] Vault composition and per-family peek budget
- [ ] Which market/timeframe is the first fully-supported profile? (Leaning `nse_equity` × `daily` — best existing data and domain knowledge)
- [ ] Do we port existing JMA+ATR work into the operator library, or rewrite clean against the new base class?
- [ ] Known-answer test corpus — which published strategies do we use as ground truth?
- [ ] Does Stage 3 ship all six markets' profiles, or one market first and the rest after M2?
- [ ] Paper trading: internal simulator vs broker paper API, per market?

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial plan. 13 stages, validation-engine-first ordering, A2/A3 before A1, 8 milestones, risk register, reuse mapping from the existing repo. |
| 2026-07-27 | Added **Stage 0 (nanoAQRL)** as the real starting point and **M0 (null-world FDR)** as the gating milestone. Added reward-hacking and selection-bias risks. Prototype code removed from the tree; reuse mapping now points at git history `d08e812`. |
