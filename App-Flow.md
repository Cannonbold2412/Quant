# App Flow — AQRL

> **Status:** Living document. Updated after every design session.
> **Last updated:** 2026-07-27
> **Companion docs:** `TRD.md` (architecture), `Backend-Schema.md` (state), `UI-UX-Brief.md` (human touchpoints)

---

## 1. The Master Loop

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│   External Knowledge          Internal Knowledge                 │
│   (papers, code, market)      (experiments, lessons, live)       │
│           │                            │                         │
│           └────────────┬───────────────┘                         │
│                        ▼                                         │
│                 ┌─────────────┐                                  │
│                 │  A1  Spec   │  Research Scientist              │
│                 └──────┬──────┘                                  │
│                        ▼                                         │
│                 ┌─────────────┐                                  │
│            ┌───►│  A2  Code   │  Quant Engineer                  │
│            │    └──────┬──────┘                                  │
│            │           ▼                                         │
│            │    ┌─────────────┐                                  │
│            │    │ evaluate.py │  P0→P1→P2→P3                     │
│            │    └──────┬──────┘                                  │
│            │           ▼                                         │
│            │    ┌─────────────┐                                  │
│            └────┤  A3 Review  │  Research Reviewer               │
│      iterate    └──────┬──────┘                                  │
│                        │ done / plateau                          │
│                        ▼                                         │
│                 ┌─────────────┐                                  │
│                 │ A4 Promote  │  Promotion Committee             │
│                 └──────┬──────┘                                  │
│                        ├──────────────► A5 Knowledge Manager ────┤
│                        ▼                                         │
│                 ╔═════════════╗                                  │
│                 ║ HUMAN GATE  ║  Dashboard review                │
│                 ╚══════┬══════╝                                  │
│                        ▼                                         │
│                  Paper Trading ──► Monitor ──► health signals ───┤
│                        ▼                                         │
│                 ╔═════════════╗                                  │
│                 ║ HUMAN GATE  ║                                  │
│                 ╚══════┬══════╝                                  │
│                        ▼                                         │
│                Live 1–5% ──► Scale ──► Monitor ──► Retire ───────┘
└──────────────────────────────────────────────────────────────────┘
```

Every arrow into and out of an agent passes through the **job queue** and the **database**. No agent calls another agent directly.

---

## 2. Flow 1 — Hypothesis Generation (A1)

**Trigger:** nightly batch, or event-driven when a research question is answered, or when a goal has unused hypothesis budget.

```
Scheduler picks up GENERATE_SPEC job
        │
        ▼
Worker assembles context (Python, not Claude):
        ├── active research_goals + allocation bucket (70/20/10)
        ├── relevant external_knowledge (vector search on goal)
        ├── relevant knowledge_entries + knowledge_edges (what we know works/fails)
        ├── open research_questions
        ├── recent failure_reasons in this family
        └── operator catalog (valid for this market + timeframe)
        │
        ▼
Claude session (A1)
        │
        ▼
Structured output: hypothesis, rationale, operator composition,
                   parameter ranges, expected_behavior, source_knowledge_ids
        │
        ▼
Compute spec_hash from the canonical operator DAG
        │
        ├── exact match exists? ──► REJECT, log duplicate, do not spend further compute
        ├── near-duplicate?      ──► attach prior result to the spec for A3's context
        └── novel                ──► INSERT strategy + strategy_spec
        │
        ▼
Emit event → enqueue IMPLEMENT job
```

**Key property:** A1 never sees raw papers or raw data. It sees pre-digested knowledge records. Its whole job is judgment about *what to investigate*.

**Anti-amnesia check:** before proposing, the worker surfaces prior failures matching the proposed operators. If knowledge says "ATR > 3.0 always overfits here," A1 receives that and must justify contradicting it.

---

## 3. Flow 2 — Implementation (A2)

**Trigger:** `IMPLEMENT` job (new spec) or `FIX_CODE` job (P0 failure) or iteration from A3.

```
Worker assembles context:
        ├── strategy_spec
        ├── research_plan (if iteration ≥ 2) — WHAT to change, not HOW
        ├── previous code_version + diff history
        ├── previous evaluation report
        ├── operator implementations available
        └── static-check rules (look-ahead patterns to avoid)
        │
        ▼
Claude session (A2) writes/edits strategy module
        │
        ▼
Static checks (Python): compile, lint, look-ahead scan, leakage scan
        │
        ├── FAIL ──► enqueue FIX_CODE with diagnostics (bounded retries)
        │            after k failures → quarantine strategy for human inspection
        │
        └── PASS ──► INSERT code_version
        │
        ▼
Emit event → enqueue EVALUATE job
```

**Boundary:** A2 implements. It does not decide research direction. If A2 believes the plan is wrong, it records the objection in `change_summary` and implements anyway — the objection surfaces to A3.

---

## 4. Flow 3 — Evaluation (`evaluate.py`, no LLM)

**Trigger:** `EVALUATE` job.

```
Resolve MarketProfile + TimeframeProfile → compute hashes
Resolve data_snapshot_id
        │
        ▼
┌── P0  Smoke & correctness ────────────────────────┐
│   compile · runs · trades > 0 · no NaN/inf        │
│   look-ahead · leakage · survivorship · PIT       │
└───────────────┬───────────────────────────────────┘
                │ FAIL → failure_reason = code_error / look_ahead_detected
                │        → FIX_CODE job (a BUG, not a research finding)
                ▼ PASS
┌── P1  Fast backtest (small slice, frictionless) ──┐
└───────────────┬───────────────────────────────────┘
                │ FAIL → no_signal → straight to A3, cheap rejection
                ▼ PASS
┌── P2  Full backtest (full history, real costs) ───┐
└───────────────┬───────────────────────────────────┘
                │ FAIL → costs_exceed_edge / negative_expectancy
                ▼ PASS
┌── P3  Robustness battery ─────────────────────────┐
│   walk-forward · Monte Carlo · deflated Sharpe    │
│   White's Reality Check · CSCV/PBO                │
│   regime analysis · cost sensitivity              │
│   parameter sensitivity · market-specific gates   │
└───────────────┬───────────────────────────────────┘
                ▼
Write evaluations + evaluation_tests + regime_performance
Write equity curve & tradebook to Parquet
Stamp provenance: eval_engine_version, profile hashes, snapshot, seed
        │
        ▼
Emit event → enqueue REVIEW job
```

### 4.1 Trial counting (critical)

Before computing deflated Sharpe, the engine queries: *how many trials have been run in this family?* — iterations of this strategy + parameter combinations swept + prior related experiments. Under-counting turns deflated Sharpe into a rubber stamp (TRD §5.2).

### 4.2 Short-circuiting

A failure at any phase skips all later phases. This is the entire economic argument of the funnel: **most ideas die in P0/P1 where they cost seconds, not in P3 where they cost hours.**

---

## 5. Flow 4 — Review & Iteration (A3)

**Trigger:** `REVIEW` job.

```
Worker assembles context:
        ├── strategy_spec (the original hypothesis)
        ├── all prior experiments for this strategy (full history)
        ├── current evaluation report + per-test results
        ├── regime breakdown
        ├── relevant knowledge_entries (has this failure been seen before?)
        └── remaining budget + iteration count + plateau counter
        │
        ▼
Claude session (A3)
        │
        ▼
Verdict:
   ├── ITERATE  → research_plan with ordered proposed_changes
   │              → enqueue IMPLEMENT (iteration n+1)
   │
   ├── PLATEAU  → no meaningful gain for N iterations
   │              → enqueue PROMOTE (A4 decides if it's still worth anything)
   │
   ├── PROMOTE  → criteria met
   │              → enqueue PROMOTE
   │
   └── REJECT   → hopeless
                  → enqueue ARCHIVE (A5) directly, skip A4
```

### 5.1 Stop conditions — evidence-based, not a fixed count

Checked by the worker *before* invoking Claude, so budget is never wasted:

| Condition | Action |
|---|---|
| All promotion criteria met | → A4 |
| No meaningful improvement for N iterations | → plateau → A4 |
| Iteration/token/compute budget exhausted | → A4 with best-so-far |
| A3 judges further modification futile | → reject → A5 |
| Hard iteration cap (backstop) | → forced plateau |

### 5.2 The research plan is not code

A3 says *"replace the fixed stop with an ATR trailing stop because exits are cutting winners in trending regimes."* It does not write the function. This keeps the scientist/engineer boundary intact and makes A3's reasoning reviewable in plain language.

---

## 6. Flow 5 — Promotion (A4)

**Trigger:** `PROMOTE` job.

```
Worker assembles the ENTIRE research history:
        ├── original hypothesis
        ├── every iteration and what changed
        ├── every evaluation report
        ├── best experiment + its full metrics
        ├── iteration_count and total_trials  ← the overfitting signal
        ├── correlation with currently-live strategies
        └── capacity/liquidity assessment
        │
        ▼
Claude session (A4)
        │
        ▼
Decision:
   ├── REJECT   → enqueue ARCHIVE (A5)
   ├── DEFER    → back to research with a new goal
   └── APPROVE  → INSERT promotion (requires_human_approval = 1)
                  → surface to dashboard
                  → enqueue ARCHIVE (A5) in parallel
```

**A4 must weigh iteration count.** A strategy that reached Sharpe 2.1 after 47 iterations is a fundamentally different object from one that hit 1.6 on the second try. More iterations = more multiple testing = higher overfitting risk, and A4 sees that explicitly.

**A4 never has trading credentials.** It produces a recommendation record. Nothing moves without the human gate.

---

## 7. Flow 6 — Knowledge Capture (A5)

**Trigger:** `ARCHIVE` job (every experiment, promoted or rejected) and weekly `MINE_PATTERNS`.

### 7.1 Per-experiment archival

```
Worker assembles: spec, all iterations, evaluations, A3 plans, A4 decision
        │
        ▼
Claude session (A5)
        │
        ▼
Produces:
   ├── lab_notebook (hypothesis / result / reason / evidence / confidence / NEXT QUESTIONS)
   ├── knowledge_entries (scope = experiment)
   ├── knowledge_edges (subject–predicate–object with evidence links)
   └── research_questions pushed to the curiosity queue
```

The **next questions are mandatory and non-empty.** An experiment that generates no follow-up questions has not been properly analyzed. This is the mechanism that makes the lab self-propelling:

```
Experiment #12,483 → rejected (overfit to 2019–2021)
        ↓
Generates: 1. Normalize ATR
           2. Try volatility clustering
           3. Test on commodities
        ↓
These become new specs in the next A1 cycle
```

### 7.2 Weekly cross-experiment pattern mining

```
A5 scans recent experiments for repeated patterns
        │
        ▼
"Experiments #25, #193, #6201 all failed with ATR > 3.0"
        │
        ▼
Promotes to a GLOBAL RULE (scope = family)
        │
        ▼
Injected into A1's context on every future spec in that family
        │
        ▼
Knowledge graph edges updated with evidence counts
```

This is the anti-amnesia machinery. Without it, the lab re-runs its own dead ends forever.

### 7.3 Curiosity loop closure

```
Failure pattern detected
        ↓
research_question created ("volatility-adaptive momentum?")
        ↓
Collectors run TARGETED search (not just broad sweeps)
        ↓
New external_knowledge extracted
        ↓
A1 consumes it → new hypothesis
        ↓
research_questions.produced_spec_ids updated
        ↓
We can now audit: did asking this question ever pay off?
```

---

## 8. Flow 7 — Human Gate 1: Research → Paper

```
Dashboard shows "Needs Review" queue
        │
        ▼
Human sees:
   ├── the hypothesis in plain language
   ├── A4's recommendation and rationale
   ├── full metrics + robustness battery results
   ├── equity curve, drawdown, regime breakdown
   ├── the ENTIRE iteration history (how much tinkering happened)
   ├── overfitting risk assessment
   └── correlation with the existing live portfolio
        │
        ├── REJECT  → back to A5 with human reasoning recorded
        ├── DEFER   → request more research (creates a new goal)
        └── APPROVE → INSERT deployment (mode = paper)
                      set expected_* baseline from validation
                      set trades_required from timeframe profile
```

---

## 9. Flow 8 — Paper Trading & Monitoring

```
Deployment active (mode = paper)
        │
        ▼
Daily MONITOR_DEPLOYMENT job
        │
        ▼
Compute health check:
   ├── performance vs expected_* baseline (z-scores)
   ├── loss distribution vs historical (p-value)
   ├── current regime + is it a historically weak one?
   ├── execution quality (slippage deviation, missed fills)
   └── trade count and regime coverage progress
        │
        ▼
Assign level:
   🟢 green  → continue
   🟡 yellow → flag, continue, notify dashboard
   🟠 orange → pause new entries, revalidate on recent data
   🔴 red    → stop, return to research pipeline, create research_question
        │
        ▼
Promotion gate check — ALL must be true (PRD §9.3):
   ✅ trades_completed ≥ trades_required
   ✅ metrics within acceptable deviation of validation
   ✅ multiple regimes observed
   ✅ no abnormal slippage/execution issues
   ✅ statistical health checks pass
        │
        ▼
All true → enqueue PROMOTE (stage paper → live_small) → Human Gate 2
```

**The gate is trades, not calendar.** A strategy trading 5×/year and one trading 5×/day need completely different elapsed times to produce the same evidence.

**Regime context matters more than drawdown.** A drawdown while in a regime where the strategy historically struggled is *expected behavior*, not evidence of death. `health_checks.regime_historically_weak` captures exactly this.

---

## 10. Flow 9 — Human Gate 2: Paper → Live, and Scaling

```
Human reviews paper-trading evidence
        │
        └── APPROVE → deployment (mode = live, allocation 1–5%)
        │
        ▼
Continuous monitoring (same health machinery, higher stakes)
        │
        ▼
Scale-up gate: sustained green + sufficient live trades
        │
        └── human approves → increase allocation stepwise
        │
        ▼
Ongoing lifecycle decisions:
   ├── continue
   ├── reduce size
   ├── return to paper
   ├── retire
   └── send back for improvement (creates a new research goal)
```

### 10.1 Hard risk controls (outside strategy logic)

Independent of any agent decision (TRD §11):
- Per-strategy max loss → automatic halt
- Per-portfolio max drawdown → automatic halt
- Position and exposure limits
- Kill switch, human-triggerable from the dashboard at any time

These exist so that **a 100% drawdown is structurally unreachable.** If a strategy ever approaches it, the failure was in the risk layer, not the research layer.

### 10.2 Retirement feeds back

A retired strategy is not deleted. It becomes a knowledge entry: what worked, for how long, in what regimes, why it decayed. Edge decay is itself a research finding.

---

## 11. Flow 10 — External Knowledge Ingestion (no LLM until the last step)

```
Scheduler fires collector jobs
        │
   ┌────┴──────┬─────────────┬──────────────┐
   ▼           ▼             ▼              ▼
arXiv/SSRN   GitHub       Blogs        Market data
(hourly)     (daily)      (hourly)     (daily post-close)
   │           │             │              │
   └───────────┴──────┬──────┴──────────────┘
                      ▼
        Dedup by content_hash · cheap relevance filter
                      ▼
        Only survivors get EXTRACT_KNOWLEDGE (LLM, once ever)
                      ▼
        Structured external_knowledge + embeddings
                      ▼
        Available to A1
```

**Claude never crawls.** Python collectors do the fetching. A document is read by an LLM exactly once in its lifetime, then never again — subsequent access is to the structured record.

**Targeted mode:** collectors also consume the `research_questions` queue, so searches are driven by the lab's own gaps rather than only broad topical sweeps.

---

## 12. Flow 11 — Scheduler Tick

The one always-running process in v1.

```
every ~60 seconds:
        │
        ├── expire dead leases → return orphaned jobs to pending
        ├── check budgets → set back-pressure flags
        ├── fire due time-based jobs (collectors, monitors, reports)
        ├── select pending jobs by (priority, scheduled_for)
        │     respecting concurrency caps and budget flags
        ├── dispatch to workers as subprocesses
        ├── reap completed workers, record cost + duration
        └── advance state machine, emit follow-on events
```

**Restart safety:** killing the scheduler mid-flight loses nothing. Claimed jobs whose leases expire return to `pending`. State lives entirely in the database.

---

## 13. Error & Edge-Case Flows

| Situation | Handling |
|---|---|
| Claude API timeout / rate limit | `failure_class = transient` → bounded retry with backoff |
| Generated code won't compile | `failure_class = deterministic` → `FIX_CODE` with diagnostics, never blind retry |
| Same strategy fails *k* times | Quarantine for human inspection; stop burning budget |
| Duplicate spec proposed | Rejected at insert via `spec_hash` unique index |
| Data snapshot missing/corrupt | Evaluation errors out; does **not** record a research failure |
| Profile or engine version changes | Affected experiments marked `comparable = 0`; optional re-evaluation queue |
| Worker dies mid-job | Lease expiry returns the job to the queue |
| Budget exhausted | Normal state — dispatch stops, reason logged, dashboard shows it |
| Human never reviews a candidate | Ages in the queue with escalating dashboard prominence; nothing auto-promotes |
| Live strategy hits red | Auto-halt per risk rules, notify human, create research question |

---

## 14. Traceability — the "why" chain

Every artifact links back to its cause. From any live trade, this chain must be walkable end to end:

```
trade
  → deployment
    → promotion (A4 rationale + human decision + notes)
      → best experiment
        → evaluation (every test, every threshold)
          → code_version (diff + change summary)
            → research_plan (A3 diagnosis + evidence cited)
              → strategy_spec (hypothesis + rationale)
                → knowledge_entries / external_knowledge that inspired it
                  → the paper or the prior experiment that produced them
```

If any link in that chain is missing, the system has violated its foundational principle (PRD §10.1).

---

## 15. Open Flow Questions

- [ ] Does A1 run as a nightly batch, purely event-driven, or both?
- [ ] Parallel iteration: may several variants of one spec be evaluated simultaneously, or is the loop strictly sequential per strategy?
- [ ] Who decides portfolio-level admission when multiple candidates clear the gate at once — A4 per-strategy, or a separate portfolio pass?
- [ ] Should P1 failures skip A3 entirely and go straight to A5 to save tokens?
- [ ] Re-evaluation policy when the engine version bumps — everything, promoted only, or on demand?

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial document. All 11 flows mapped, evidence-based stop conditions, trial counting, curiosity loop closure, two human gates, error/edge cases, traceability chain. |
