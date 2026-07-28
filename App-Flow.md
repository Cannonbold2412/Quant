# App Flow — AQRL

> **Status:** Design complete for v1. No implementation started.
> **Last updated:** 2026-07-28
> **Companion docs:** `PRD.md` (why) · `TRD.md` (architecture) · `Backend-Schema.md` (state) · `UI-UX-Brief.md` (human touchpoints)

---

## 1. The Master Loop

```
        External Knowledge                 Internal Knowledge
        (papers, code, market)             (experiments, lessons, live results)
                 │                                    │
                 └──────────────┬─────────────────────┘
                                ▼
                    ┌───────────────────────┐
                    │  A1  Research Scientist│  → strategy spec
                    └───────────┬───────────┘
                                ▼
                    ┌───────────────────────┐
               ┌───▶│  A2  Quant Engineer   │  → code
               │    └───────────┬───────────┘
               │                ▼
               │        ┌──────────────┐
               │        │ evaluate.py  │  hard bar, then P0→P3
               │        └───────┬──────┘
               │                ▼
               │        bar cleared? ──── YES ─────────────┐
               │                │ NO                       │
               │                ▼                          ▼
               │    ┌───────────────────────┐   ┌──────────────────────┐
               └────┤  A3  Research Reviewer│   │ A4 Promotion Committee│
       iterate      └───────────┬───────────┘   └──────────┬───────────┘
                                │ plateau / reject          │
                                ▼                           ▼
                    ┌───────────────────────┐        ╔═════════════╗
                    │  A5  Knowledge Manager│◀───────║ HUMAN GATE  ║
                    └───────────┬───────────┘        ╚══════┬══════╝
                                │                           ▼
                                │                    Paper Trading
                                │                           ▼
                                │                    ╔═════════════╗
                                │                    ║ HUMAN GATE  ║
                                │                    ╚══════┬══════╝
                                │                           ▼
                                │              Live 1–5% → Scale → Monitor → Retire
                                │                           │
                                └───────────┬───────────────┘
                                            ▼
                                  better hypotheses ───↺
```

**Two invariants hold everywhere in this document:**

1. **Every arrow passes through the job queue and the database.** No agent ever calls another agent. An agent writes a row; the scheduler notices; the scheduler dispatches the next job.
2. **Clearing the bar skips A3 entirely and goes straight to A4** (§6.1) — the first passing iteration is the last iteration, by rule, not by any agent's judgment.

**Every experiment reaches A5**, whether it succeeded or failed. Nothing is discarded without a lesson.

---

## 2. Flow 0 — the nanoAQRL loop ★

**This is what actually runs first.** Everything in §3–§15 is the destination; this is the starting point (TRD §2). One agent, one editable file, no queue, no orchestration.

```
      program.md  (human-written: goal, rules, acceptance bar)
            │
            ▼
   ┌──▶ agent edits strategy.py
   │        │
   │        ▼
   │    git commit
   │        │
   │        ▼
   │    run evaluate.py   ← agent can neither read nor edit this
   │        │
   │        ▼
   │    HARD BAR (enforced here, not in program.md)
   │    min trades · max OOS drawdown · breadth · 2× costs · complexity
   │        │
   │   fail ├────────────────▶ discard, NO score computed
   │        │
   │   pass ▼
   │    rolling walk-forward (1yr test, purged + embargo, 2× costs)
   │    all folds CONCATENATED into one OOS series
   │        │
   │        ▼
   │    honest_score = SR_oos − 2·SE(SR) − SR*(N_trials)
   │        │
   │        ▼
   │    keep the commit — and STOP (satisficing, PRD §10.2)
   │        │
   │        ▼
   │    append row to results.tsv
   │    insert row into experiments (SQLite, with code_commit)
   │        │
   └────────┘   below-bar attempts repeat, unattended, no human interruption
```

### 2.1 Rules

- **`strategy.py` is the only writable file.** `data.py` is read-only; `evaluate.py` is neither readable nor writable.
- **The bar is enforced in `evaluate.py`, not merely stated in `program.md`** (TRD §7.5). `program.md` tells the agent what it is aiming at; `evaluate.py` decides whether it got there. Otherwise the agent grades its own homework.
- **One float drives the loop.** All other metrics are computed and stored, but only `honest_score` decides keep vs discard.
- **`program.md` is human-edited.** As the agent makes avoidable mistakes, the human adds a line. That file — not an agent-maintained knowledge base — is where accumulated wisdom lives in v1. Required contents in TRD §2.4.
- **Every run gets a status:** `keep` · `discard` · `crash`. No result goes unjudged.
- **Do not stop to ask the human.** Human gates exist only at paper trading and live capital.
- **Stop on satisficing, not maximising.** The first strategy clearing the pre-set bar wins.

### 2.2 What this flow deliberately omits

No A1/A3/A4/A5, no Librarian, no job queue, no scheduler, no knowledge graph, no research plans, no promotion committee. Each is added when a **specific pain is felt** — the loop plateaus, a question arrives that `results.tsv` cannot answer, one agent visibly fails at one job. Never on a schedule.

---

## 3. Flow 1 — Hypothesis Generation (A1)

### 3.1 A1 is stateless — there is no "since last time" ★

**A1 has no memory between runs.** Every `GENERATE_SPEC` job is a brand-new Claude session that knows nothing about any prior run (TRD §1). So *"does it look at new knowledge or old knowledge?"* has a specific answer: **neither exclusively — every run searches the whole combined pool, old and new mixed together, fresh, every time.**

There is no separate "new" bucket A1 tracks. Freshly written `external_knowledge` rows simply become part of the same searchable pool the next time anyone queries it.

**Three triggers, all producing the same job type:**

| Trigger | When |
|---|---|
| **Nightly batch** | One job per active `research_goal` with unused hypothesis budget |
| **Curiosity closure** | A `research_question` gets answered by new external knowledge |
| **Novelty push** ★ | The Librarian writes an `external_knowledge` row with `novelty_score` above threshold → enqueues `GENERATE_SPEC` directly, so a standout idea doesn't wait for the nightly batch |

### 3.2 The Research Brief — what A1 actually receives ★

The worker does **not** hand A1 the database. It hands A1 a small, targeted packet built by **relevance search** (vector similarity + structured filters on market/timeframe/category), not a full table scan. At scale this is the only thing that keeps a job cheap regardless of how large the knowledge base has grown — and it also makes A1 *better*, since burying the 10 relevant items under 49,990 irrelevant ones degrades reasoning.

```
Scheduler picks up GENERATE_SPEC job
        │
        ▼
Worker assembles the RESEARCH BRIEF (Python, not Claude):
        ├── the research_goal + its allocation bucket (70/20/10)
        ├── top-K relevant external_knowledge      ← candidates, UNTESTED
        ├── top-K relevant knowledge_entries + edges ← tested, TRUSTED
        ├── open research_questions for this goal
        ├── recent failure_reasons in this family     (anti-amnesia)
        ├── operator catalog valid for this market/timeframe
        └── trials already spent in this family        (feeds the trials haircut)
        │
        ▼
Claude session (A1) — reads the brief, proposes ONE spec
```

### 3.3 Combination happens in reasoning, not in a database join

The worker's only job is getting the *right* old (trusted) and new (candidate) items into the same brief. Noticing a useful combination between them is exactly what the LLM is for:

```
OLD, trusted (knowledge_entries):
  "ATR multiplier > 3.0 overfits in this NSE trend family"

NEW, untested (external_knowledge, from the Librarian):
  "Volatility-normalized position sizing reduces drawdown in trending regimes"

→ A1's proposal: replace the fixed ATR multiplier with volatility-normalized
  sizing — same purpose, a mechanism that sidesteps the known failure mode,
  incorporating the new idea while respecting the old lesson.
```

**Anti-amnesia check:** before proposing, the worker surfaces prior failures matching the proposed operators. If knowledge says "ATR > 3.0 always overfits here," A1 receives that and must **justify contradicting it.**

### 3.4 A1's output

```
Structured output (→ strategy_specs, Backend-Schema §4):
        hypothesis                      — one falsifiable sentence
        rationale                        — why this is worth trying
        entry_logic / exit_logic /       — operator DAG, composed only from
          filter_logic / risk_logic         the vetted library
        universe                          — instrument selection
        parameters                        — names, defaults, allowed ranges
        expected_behavior                — A1's prediction, scored later for calibration
        source_external_knowledge_ids    — which candidate ideas it drew on
        source_internal_knowledge_ids    — which tested lessons it respected/avoided
        prompt_version
        │
        ▼
Compute spec_hash from the canonical operator DAG
        │
        ├── exact match exists? ──▶ REJECT, log duplicate, spend no further compute
        ├── near-duplicate?      ──▶ attach the prior result to the spec for A3's context
        └── novel                ──▶ INSERT strategy + strategy_spec
        │
        ▼
Emit event → enqueue IMPLEMENT job
```

**Key property:** A1 never sees a raw paper or raw market data — only pre-digested, structured records from both knowledge bases. Its whole job is judgment about **what to investigate next.**

---

## 4. Flow 2 — Implementation (A2)

**Trigger:** the previous flow's *write* is the trigger. A `strategy_specs` insert (§3) or an A3 `research_plans` insert (§6) fires an event; the scheduler turns it into an `IMPLEMENT` job. A2 never receives a call from A1 or A3 — it picks up a job the scheduler queued because it noticed a row appear.

### 4.1 The Implementation Brief

```
Worker assembles:
        ├── strategy_spec (hypothesis, operator DAG, parameters, universe)
        ├── research_plan (if iteration ≥ 2) — WHAT to change, not HOW
        ├── previous code_version + diff history
        ├── previous evaluation report
        ├── operator implementations (actual code, so it knows how to call each block)
        └── anti-look-ahead rules from program.md (TRD §2.4)
```

**A2 never receives `evaluate.py`.** It knows the correctness rules it must follow, but not how its score will be computed — an agent that can see the scorer eventually aims at the scorer instead of the market.

### 4.2 What A2 does — translation, not invention

It assembles operator-library building blocks into runnable code exactly as the spec describes. It is not free to invent new logic:

```
entry_logic: "JMA slope turns positive AND ATR expands beyond 20-day average"
        ↓
def entry_signal(df):
    jma = jma_slope(df.close, period=14)
    atr_exp = atr_expansion(df, lookback=20)
    return (jma > 0) & atr_exp
```

### 4.3 A2's output

```
On a strategy's FIRST implement job: create branch strategy/<strategy_id> (TRD §5.2)
        │
        ▼
INSERT code_versions row (Backend-Schema §5):
        code_path, code_hash, git_commit
        diff_from_parent        — empty on iteration 1
        change_summary          — plain language: what changed and why
        implements_plan_id      — which research_plan this responds to
        compile_ok, static_check_results
        │
        ▼
Static checks (Python): compile, lint, look-ahead scan, leakage scan
        │
        ├── FAIL ──▶ enqueue FIX_CODE with diagnostics (bounded retries)
        │            a static-check failure is a BUG, never a research finding
        │            after k failures → quarantine for human inspection
        │
        └── PASS ──▶ Emit event → enqueue EVALUATE job
```

**Boundary:** A2 implements; it does not decide research direction. If A2 believes the plan is wrong, it records the objection in `change_summary` and implements anyway — the objection surfaces to A3, never acted on unilaterally.

---

## 5. Flow 3 — Evaluation (`evaluate.py`, no LLM)

**Trigger:** `EVALUATE` job. This flow contains no LLM call at any point.

```
Resolve MarketProfile + TimeframeProfile + wf_config → compute hashes
Resolve data_snapshot_id
        │
        ▼
┌── HARD BAR (pre-registered, TRD §7.5) ────────────┐
│   min trades · max OOS drawdown · breadth         │
│   2× cost survival · max complexity               │
└───────────────┬───────────────────────────────────┘
                │ FAIL → discard, NO score computed, stop here
                ▼ PASS
┌── P0  Smoke & correctness ────────────────────────┐
│   compile · runs · trades > 0 · no NaN/inf        │
│   look-ahead · leakage · survivorship · PIT       │
└───────────────┬───────────────────────────────────┘
                │ FAIL → failure_reason = code_error / look_ahead_detected
                │        → FIX_CODE job (a BUG, not a research finding)
                ▼ PASS
┌── P1  Fast backtest (small slice, frictionless) ──┐
└───────────────┬───────────────────────────────────┘
                │ FAIL → no_signal → cheap rejection
                ▼ PASS
┌── P2  Full backtest (full history, real costs) ───┐
└───────────────┬───────────────────────────────────┘
                │ FAIL → costs_exceed_edge / negative_expectancy
                ▼ PASS
┌── P3  Robustness battery ─────────────────────────┐
│   rolling walk-forward → concatenated OOS series  │
│   Monte Carlo · deflated Sharpe · White's RC      │
│   CSCV/PBO · regime analysis · cost sensitivity   │
│   parameter sensitivity · market-specific gates   │
└───────────────┬───────────────────────────────────┘
                ▼
honest_score = SR_oos − 2·SE(SR) − SR*(N_trials)
        │
        ▼
Write evaluations + evaluation_tests + regime_performance + fold_metrics
Write equity curve & tradebook to Parquet
Stamp provenance: engine version, profile hashes, wf_config_hash, snapshot, seed
        │
        ▼
   bar cleared?
        │
   ┌────┴─────┐
  YES         NO
   │            │
   ▼            ▼
enqueue      enqueue
PROMOTE      REVIEW
(A4)         (A3)
```

### 5.1 Trial counting — critical

Before computing the deflated Sharpe, the engine queries: *how many trials have been run in this **family**?* — iterations of this strategy, plus parameter combinations swept, plus prior related experiments including the same idea tried in other markets. **Under-counting turns the deflated Sharpe into a rubber stamp** (TRD §10.2).

### 5.2 Short-circuiting is the economic argument

A failure at any phase skips all later phases. **Most ideas die at the bar or in P0/P1 where they cost seconds, not in P3 where they cost hours.**

---

## 6. Flow 4 — Review & Iteration (A3)

**Trigger:** `REVIEW` job — fired **only** when the evaluation failed the hard bar. The worker checks `bar_result` the instant `evaluate.py` returns, before deciding whether to invoke Claude at all.

### 6.1 Clearing the bar is an immediate, unconditional stop ★

**The moment any iteration clears the acceptance bar, iteration on that strategy stops — permanently, right then.** Not "stop if it also fails to improve further." Not "try a few more to see if it can do better." The first pass is the last iteration, and A3 is not invoked for that decision at all — the worker checks `bar_result` in Python and routes straight to `PROMOTE`.

```
evaluate.py returns
        │
        ▼
   bar_result?
        │
   ┌────┴────┐
  PASS       FAIL
   │            │
   ▼            ▼
enqueue      enqueue REVIEW (A3) — the rest of this flow
PROMOTE      │
(A4) —       ▼
skip A3   A3 decides: ITERATE, or give up (§6.2)
entirely
```

**Why this is a rule and not a judgment call:** this *is* satisficing (PRD §10.2). Leaving "should I keep pushing for a higher score?" as something A3 could decide would let an LLM quietly override the whole principle, one plausible-sounding justification at a time. So it is not offered as a choice: the worker enforces it before Claude is ever asked.

**And there is nothing left to optimise for.** `acceptance_bars.min_score` is already part of the bar, so clearing it already means *"good enough by the standard set in advance."* Continuing would only spend more of this family's trial budget and more of its irreplaceable out-of-sample data (TRD §7.8) chasing a number nobody asked for.

### 6.2 Below the bar — where A3 actually operates

Everything past this point only ever happens **before** a strategy has cleared the bar.

```
Worker assembles context:
        ├── strategy_spec (the original hypothesis)
        ├── all prior experiments for this strategy (full history)
        ├── current evaluation — bar_failed_on + the raw diagnostic metrics
        │      (no honest_score exists on a bar failure — TRD §7.5)
        ├── regime breakdown
        ├── relevant knowledge_entries (has this failure been seen before?)
        └── remaining budget + iteration count + consecutive-bar-failure count
        │
        ▼
Claude session (A3)
        │
        ▼
Verdict:
   ├── ITERATE  → research_plan with ordered proposed_changes
   │              → enqueue IMPLEMENT (iteration n+1)
   │
   ├── PLATEAU  → 5 consecutive bar failures, never once cleared (§6.3)
   │              → enqueue ARCHIVE (A5), skip A4
   │              (failure_reason = plateaued_below_bar)
   │
   └── REJECT   → hopeless before even reaching patience
                  → enqueue ARCHIVE (A5), skip A4
```

**Note what is missing:** there is **no `PROMOTE` verdict** here, and no path from this flow to A4. The only route to A4 is clearing the bar (§6.1). A3's job below the bar is narrower than it looks — decide whether to try again, or give up.

#### Stop conditions, all checked before invoking Claude

| Condition | Action |
|---|---|
| **Bar cleared** | → A4 immediately; A3 not invoked (§6.1) |
| **5 consecutive bar failures**, never cleared | → PLATEAU → A5, `plateaued_below_bar` |
| Budget exhausted before ever clearing the bar | → A5, same reason |
| A3 judges further modification futile | → REJECT → A5 |
| Hard iteration cap (backstop, default ~20–25) | → forced PLATEAU → A5 |

### 6.3 Plateau — only ever a below-the-bar concept

Because clearing the bar is an immediate stop, **there is never more than one passing evaluation for a strategy.** So there is nothing to compare passing scores against, and no notion of "improvement between passing attempts" is needed. Plateau counting is purely about the climb *toward* the bar:

```
Each iteration that FAILS the bar:   consecutive_bar_failures += 1
Each iteration that CLEARS the bar:  → immediate PROMOTE. Counting never resumes.

5 consecutive bar failures, never having cleared it → PLATEAU
```

A bar-failing evaluation has **no `honest_score`** to compare (the score is only computed after the bar passes), so A3 reasons over the raw diagnostic instead: `bar_failed_on` and the underlying metrics that fed that check — how far below `min_trades`, how far over `max_drawdown`. That is enough to distinguish "getting closer" from "stuck" without needing a synthetic score for failing attempts.

**PLATEAU always routes to A5, never to A4.** There is nothing bar-passing for A4 to review.

**Configurable per campaign** via `acceptance_bars.plateau_patience` (default 5). The hard iteration cap remains an outer backstop.

### 6.4 The research plan is not code

A3 says *"replace the fixed stop with an ATR trailing stop, because exits are cutting winners short in trending regimes."* It does not write the function. This keeps the scientist/engineer boundary intact and makes A3's reasoning reviewable in plain language.

---

## 7. Flow 5 — Promotion (A4)

**Trigger:** `PROMOTE` job — fired the instant an evaluation clears the bar (§6.1). A3 need not have been involved at all; A4 reads the winning evaluation directly.

```
Worker assembles the ENTIRE research history:
        ├── original hypothesis
        ├── every iteration and what changed
        ├── every evaluation report — including all the bar-FAILING attempts
        ├── the passing experiment + its full metrics
        ├── iteration_count and total_trials  ← the overfitting signal
        └── capacity/liquidity assessment — can THIS strategy trade at real size, alone
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

### 7.1 A4 must weigh iteration count

A strategy that cleared the bar on iteration 31 of a 47-try grind is a fundamentally different object from one that cleared it on try 2. **More attempts = more multiple testing = higher overfitting risk**, and A4 sees that explicitly.

### 7.2 A4 does not check portfolio correlation ★

That question — *"is this a genuinely new source of profit, or the same bet I already hold?"* — is portfolio-construction work, and multi-strategy portfolio construction is explicitly out of scope for v1 (PRD §3). A4 judges a strategy **on its own merits only**: is the evidence real, is it tradeable, how much tinkering did it take.

Correlation still reaches the human, but computed independently by the dashboard (§9) — deliberately *more* than A4 itself saw.

### 7.3 A4 can say no alone, never yes alone

| Decision | Needs a human? |
|---|---|
| REJECT | No — A4's own authority |
| DEFER | No — A4's own authority |
| **APPROVE** | **Always.** Produces a recommendation only |

**A4 never has trading credentials.** Nothing moves without the human gate.

---

## 8. Flow 6 — Knowledge Capture (A5)

**Trigger:** `ARCHIVE` job — for **every** experiment, promoted or rejected — plus a weekly `MINE_PATTERNS` job.

### 8.1 Per-strategy archival

A5 runs **once, when a strategy's story concludes** — not after each iteration. The raw record of every attempt was already written automatically as it happened (TRD §12.1), so A5 reads the *complete* set at once and writes one well-formed lesson rather than five half-formed ones.

```
Worker assembles: spec, ALL iterations, all evaluations, A3 plans, A4 decision
        │
        ▼
Claude session (A5)
        │
        ▼
Produces:
   ├── lab_notebook      (hypothesis / result / reason / evidence / confidence /
   │                       NEXT QUESTIONS — mandatory, non-empty)
   ├── knowledge_entries (scope = experiment)
   ├── knowledge_edges   (subject–predicate–object, each backed by experiments)
   └── research_questions pushed to the curiosity queue
```

**Next questions are mandatory and non-empty.** An experiment generating no follow-up has not been properly analysed. This is the mechanism that makes the lab self-propelling:

```
Experiment #12,483 → rejected (overfit to 2019–2021)
        ↓
Generates: 1. Normalise ATR
           2. Try volatility clustering
           3. Test on commodities
        ↓
These become new specs in the next A1 cycle
```

### 8.2 Weekly cross-experiment pattern mining

```
A5 scans recent experiments for repeated patterns
        ↓
"Experiments #25, #193, #6201 all failed with ATR > 3.0"
        ↓
Promotes to a FAMILY-scoped rule
        ↓
Injected into A1's Research Brief for every future spec in that family
        ↓
Knowledge graph edges updated with evidence counts
```

**This is the anti-amnesia machinery.** Without it, the lab re-runs its own dead ends forever — and the repeat-failure rate (PRD §4.4) is how we know whether it is working.

### 8.3 Curiosity loop closure

```
Failure pattern detected
        ↓
research_question created ("volatility-adaptive momentum?")
        ↓
Collectors run a TARGETED search (not just broad sweeps)
        ↓
Librarian extracts new external_knowledge
        ↓
A1 consumes it → new hypothesis
        ↓
research_questions.produced_spec_ids updated
        ↓
Auditable: did asking this question ever pay off?
```

---

## 9. Flow 7 — Human Gate 1: Research → Paper

```
Dashboard shows "Needs Review" queue
        │
        ▼
Human sees:
   ├── the hypothesis in plain language
   ├── A4's recommendation and rationale
   ├── full metrics + robustness battery results, every threshold visible
   ├── equity curve, drawdown, regime breakdown
   ├── the ENTIRE iteration history (how much tinkering happened)
   ├── overfitting risk assessment
   └── correlation with the existing live portfolio
         ★ computed fresh by plain Python from stored return series —
           NOT part of A4's brief or decision (§7.2). A4 judges the
           strategy alone; the human deliberately gets more than A4 saw
        │
        ├── REJECT  → back to A5 with the human's reasoning recorded
        ├── DEFER   → request more research (creates a new goal)
        └── APPROVE → INSERT deployment (mode = paper)
                      set expected_* baseline from validation
                      set trades_required from the timeframe profile
                      merge strategy/<id> → deploy/paper (TRD §5.3)
                      merge commit references this promotion's ID
```

**Approval requires a typed note.** Friction on purpose — it forces the human to articulate why, and it becomes the data for evaluating A4's calibration later.

---

## 10. Flow 8 — Paper Trading & Monitoring

```
Deployment active (mode = paper)
        │
        ▼
Daily MONITOR_DEPLOYMENT job
        │
        ▼
Compute health check:
   ├── performance vs expected_* baseline (z-scores, not raw comparison)
   ├── loss distribution vs historical (p-value)
   ├── current regime + is it one this strategy historically struggles in?
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
   ✅ no abnormal slippage / execution issues
   ✅ statistical health checks pass
        │
        ▼
All true → enqueue PROMOTE (paper → live_small) → Human Gate 2
```

**The gate is trades, not calendar.** A strategy trading 5×/year and one trading 5×/day need completely different elapsed times to produce the same evidence.

**Regime context matters more than drawdown.** A drawdown occurring in a regime where the strategy historically struggled is *expected behaviour*, not evidence of death. This single check prevents the most common bad decision — killing a healthy strategy for an expected drawdown.

---

## 11. Flow 9 — Human Gate 2: Paper → Live, and Scaling

```
Human reviews paper-trading evidence
        │
        └── APPROVE → deployment (mode = live, allocation 1–5%)
                       merge strategy/<id> → deploy/live (TRD §5.3)
                       — the second and last merge this strategy ever gets
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

### 11.1 Hard risk controls, outside strategy logic

Independent of any agent decision (TRD §17):

- Per-strategy max loss → automatic halt
- Per-portfolio max drawdown → automatic halt
- Position and exposure limits
- Kill switch, human-triggerable from the dashboard at any time

These exist so that **a 100% drawdown is structurally unreachable.** If a strategy ever approaches it, the failure was in the risk layer, not the research layer.

### 11.2 Retirement feeds back

A retired strategy is not deleted. It becomes a knowledge entry: what worked, for how long, in what regimes, why it decayed. **Edge decay is itself a research finding.**

**Retirement removes the strategy from `deploy/live` (or `deploy/paper`), never from `strategy/<id>`.** The deploy branches answer "what is running right now," so a retired strategy must leave them; the research branch keeps the full history forever (TRD §5.3).

---

## 12. Flow 10 — External Knowledge Ingestion (the Librarian)

**One unified pipeline for every source type.** A GitHub source contributes its text (README, docs, comments) through the exact same path as a paper or a blog post — no separate tooling branch for code.

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
        (before ANY LLM cost is spent)
                      ▼
        Survivors only → THE LIBRARIAN
                      │
              ┌───────┴────────┐
              │  big document?  │
              └───────┬────────┘
                  yes  │  no
                       ▼
          chunk by STRUCTURE (section/heading — never a
          blind token window, so a formula is never split)
                       ▼
          PASS 1 — per chunk: what claim/method is here?
          → document_chunks.chunk_extraction
                       ▼
          PASS 2 — synthesize ACROSS all chunks of this
          document into a few DISTINCT ideas
          (typically 2–3 per paper, never one blob)
                       ▼
          classify + tag extraction_confidence
          + evidence_tier = 'external_claim' (TRD §12.3)
                       ▼
        One external_knowledge row PER IDEA, each carrying
        source_chunk_ids back to the exact passage
                       ▼
        Available to A1 — as a candidate to test, never a fact
```

**Claude never crawls.** Python collectors fetch. A document is read by the Librarian **exactly once in its lifetime**, then never again — every subsequent access is to the structured rows.

**Targeted mode:** collectors also consume the `research_questions` queue, so searches are driven by the lab's own gaps rather than only broad topical sweeps.

**The Librarian runs outside the five-agent loop.** Never invoked by A3 or A4, never blocks an experiment — it only adds candidates to the shelf A1 reads from next cycle.

---

## 13. Flow 11 — Scheduler Tick

The one always-running process.

```
every ~60 seconds:
        │
        ├── expire dead leases → return orphaned jobs to pending
        ├── check budgets → set back-pressure flags
        ├── fire due time-based jobs (collectors, monitors, reports)
        ├── select pending jobs by (priority, scheduled_for),
        │     respecting concurrency caps and budget flags
        ├── dispatch to workers as subprocesses
        ├── reap completed workers, record cost + duration
        └── advance the state machine, emit follow-on events
```

**Restart safety:** killing the scheduler mid-flight loses nothing. Claimed jobs whose leases expire return to `pending`. **State lives entirely in the database.**

---

## 14. Flow 12 — Null-World Calibration (runs before anything real) ★

The procedure that establishes whether the pipeline can be trusted at all (PRD §4.2, TRD §14.3).

```
Generate null datasets — NO alpha by construction
   permuted returns · block bootstrap · synthetic GBM · matched-vol fat-tail paths
            │
            ▼
Run the COMPLETE loop against them
   generation → iteration → evaluation → promotion recommendation
   (identical code path to real data — no shortcuts, no special-casing)
            │
            ▼
Count reported "discoveries"
            │
      ┌─────┴─────────────────┐
   0 found                 12 found
      │                       │
      ▼                       ▼
 pipeline_trusted        pipeline_suspect
 proceed to real data    fix evaluate.py / the scoring rule,
                         then re-run. Do NOT proceed.
            │
            ▼
Record null_world_runs · surface FDR on the Laboratory screen
Record max_score_observed → the bar any real result must clear
```

**Re-run as a regression test** after every change to `evaluate.py`, the scoring rule, or any profile. Throughput is tied to the result via the autonomy ratchet (TRD §14.4): if FDR rises, experiments-per-day automatically fall.

---

## 15. Flow 13 — Vault Access (the only path to real capital) ★

```
Strategy clears the acceptance bar on searchable data
            │
            ▼
A4 / human requests promotion
            │
            ▼
Check vault budget for this FAMILY (not this strategy)
            │
      ┌─────┴──────┐
  budget = 0   budget > 0
      │            │
      ▼            ▼
  BLOCKED      open the vault segment  ← logged, decrements budget
  until new         │
  data exists       ▼
             score on data the loop never touched
                    │
              ┌─────┴──────┐
          confirmed    contradicted
              │            │
              ▼            ▼
        human gate    reject; record as knowledge;
                      the family's budget is already spent
```

**The loop has no read path to the vault** — not "must not," *cannot*. Every other protection depends on honestly counting trials, which becomes unknowable once hypotheses are influenced by memory of past results. **This is the one defence that does not depend on counting anything.**

---

## 16. Error & Edge-Case Flows

| Situation | Handling |
|---|---|
| Claude API timeout / rate limit | `failure_class = transient` → bounded retry with backoff |
| Generated code won't compile | `failure_class = deterministic` → `FIX_CODE` with diagnostics, never blind retry |
| Same strategy fails *k* times | Quarantine for human inspection; stop burning budget |
| Duplicate spec proposed | Rejected at insert via the `spec_hash` unique index |
| Data snapshot missing/corrupt | Evaluation errors out; does **not** record a research failure |
| Profile / engine / wf-config version changes | Affected experiments marked `comparable = 0`; optional re-evaluation queue |
| Experiment exceeds its time budget | Killed, recorded as `crash`, not `discard` (TRD §9.6) |
| Worker dies mid-job | Lease expiry returns the job to the queue |
| Budget exhausted | Normal state — dispatch stops, reason logged, dashboard shows it |
| Human never reviews a candidate | Ages in the queue with escalating prominence; **nothing auto-promotes** |
| Live strategy hits red | Auto-halt per risk rules, notify human, create a research question |

---

## 17. Traceability — the "why" chain

Every artifact links back to its cause. From any live trade, this chain must be walkable end to end:

```
trade
  → deployment
    → promotion (A4 rationale + human decision + typed note + merge commit)
      → the passing experiment
        → evaluation (every test, every threshold, full provenance)
          → code_version (git commit + diff + change summary)
            → research_plan (A3 diagnosis + evidence cited)
              → strategy_spec (hypothesis + rationale)
                → knowledge_entries / external_knowledge that inspired it
                  → document_chunks → the exact passage in the source paper
```

**If any link in that chain is missing, the system has violated its foundational principle** (PRD §11.1).

---

## 18. Open Flow Questions

- [ ] Does A1 run as a nightly batch, purely event-driven, or both?
- [ ] Parallel iteration: may several variants of one spec be evaluated simultaneously, or is the loop strictly sequential per strategy?
- [ ] Should P1 failures skip A3 entirely and go straight to A5 to save tokens?
- [ ] Re-evaluation policy when the engine version bumps — everything, promoted only, or on demand?
- [ ] Who decides portfolio-level admission when multiple candidates clear the gate at once — deferred with the rest of portfolio construction (PRD §3)

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial document — all flows mapped, evidence-based stop conditions, trial counting, curiosity loop closure, two human gates, error/edge cases, traceability chain. |
| 2026-07-27 | Added Flow 0 (the nanoAQRL loop that actually runs first), null-world calibration and vault access flows. Rewrote Flow 1 around the Research Brief and Flow 2 around the Implementation Brief; rewrote Flow 10 around the Librarian. |
| 2026-07-28 | Clearing the bar became an immediate stop routing straight to A4; A3 lost its `PROMOTE` verdict entirely; plateau collapsed to a single below-the-bar concept always routing to A5. Portfolio correlation removed from A4 and marked dashboard-computed. Git merge actions wired into both human gates. |
| 2026-07-28 | **Full rewrite for clarity and consistency.** Sequential numbering (§1–§18) replacing the patched §1A/§5.0/§5.1a/§7a scheme; the hard bar now shown explicitly in the evaluation flow where it actually runs; A5 documented as running once per strategy over the complete iteration set; all cross-references updated to the renumbered TRD and PRD. No decisions changed in this pass. |
