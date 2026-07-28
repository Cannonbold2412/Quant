# Implementation Plan — AQRL

> **Status:** Design complete. **Nothing here is built yet.**
> **Last updated:** 2026-07-28
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
| **0.1** | **Implement the honest score** — `SR_oos − 2·SE(SR) − SR*(N_trials)` (TRD §7) | Rolling walk-forward, test window fixed at 1 year, train window 1/2/3 years chosen before the campaign (default 1), purged, embargo ≥ holding period, 2× costs. All folds **concatenated** into one OOS series. Returns one float |
| **0.2** | **Build `evaluate.py`** around it, with the **hard bar enforced inside it** | Structurally isolated: the agent can neither read nor edit it. The bar gates before any score is computed |
| **0.3** | **Run the null-world test** (TRD §14.3) | Prove the scorer does not invent discoveries in pure noise. Fix and re-run until FDR is low |
| **0.4** | **Build the vault** (TRD §14.2) | Lock the holdout *before* the loop ever touches real data |
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
| DB access layer | Thin repository pattern; **no SQLite-specific SQL** (TRD §19) |
| `MarketProfile` / `TimeframeProfile` loaders | YAML → validated object → content hash |
| First profiles | `nse_equity` × `{daily, 15min}` — the market we know best |
| Data snapshot manager | Ingest CSV/Parquet → immutable versioned snapshot + content hash |
| Per-market data validators | Run **at ingest**, not at experiment time |
| Structured logging | Correlation IDs threading `strategy → experiment → job` |

**Done when:** a market snapshot can be ingested, versioned, hashed and loaded by ID; profiles resolve and hash deterministically.

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

---

## 6. Stage 4 — Nervous System

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

## 8. Stage 5 — A2 Quant Engineer

**Goal:** spec → working code.

| Deliverable | Notes |
|---|---|
| Claude session wrapper | Stateless, structured output, schema-validated |
| Context assembler | **Python builds the brief** — spec, research plan, prior code + diff, prior evaluation, operator catalog |
| Branch creation | On a strategy's first `IMPLEMENT` job: create `strategy/<strategy_id>` (TRD §5.2) |
| Code generation | Emits a strategy module composed from operators |
| Sandboxed execution | No network, no credentials (TRD §17) |
| Static check pipeline | Compile, lint, look-ahead scan — before evaluation is even queued |
| `FIX_CODE` path | Deterministic failures return with diagnostics attached |
| Prompt versioning | Recorded on every output |

**Done when:** given a hand-written spec, A2 produces code that passes P0 and runs through `evaluate.py` unattended.

---

## 9. Stage 6 — A3 Research Reviewer ★ CLOSES THE LOOP

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
| Vault access gate | Opening the vault is logged and decrements the family budget (TRD §14.2) |

**Done when:** a human can make a fully-informed gate decision from the terminal. The dashboard is deferred — a CLI is sufficient to validate the loop.

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
| **Reward hacking — the agent optimises the scorer, not the market** | 🔴 Critical | `evaluate.py` unreadable and unwritable; `data.py` read-only; too-good-to-be-true tripwire; periodic red-teaming (TRD §14.1) |
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
- [ ] Known-answer test corpus — which published strategies serve as ground truth?
- [ ] Does Stage 3 ship all six markets' profiles, or one market first and the rest after M2?
- [ ] Paper trading: internal simulator vs broker paper API, per market?

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial plan — 13 stages, validation-engine-first ordering, A2/A3 before A1, milestones, risk register, reuse mapping. |
| 2026-07-27 | Added Stage 0 (nanoAQRL) as the real starting point and M0 (null-world FDR) as the gating milestone. Added reward-hacking, selection-bias and performance risks. |
| 2026-07-28 | Added Stage 4a (Observability), pulled out of the Stage 12 dashboard and placed immediately after the job queue. Rewrote Stage 10 around the Librarian. |
| 2026-07-28 | Stage 6 gained the bar-clear short-circuit and lost A3's `promote` verdict; Stage 8's A4 lost the portfolio-correlation deliverable; Stage 5 and 9 gained the git branch and merge-on-approve steps. |
| 2026-07-28 | **Full rewrite for clarity and consistency.** Sequential section numbering; Stage 0 expanded with the parallelisation step in its proper order; all cross-references updated to the renumbered TRD, PRD and App-Flow; changelog consolidated. No plan decisions changed in this pass. |
