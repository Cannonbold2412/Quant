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
│            │      bar cleared? ── YES ─────────────────┐         │
│            │           │ NO                             │         │
│            │           ▼                                │         │
│            │    ┌─────────────┐                          │        │
│            └────┤  A3 Review  │  Research Reviewer        │        │
│      iterate    └──────┬──────┘                            │      │
│                        │ plateau/reject → A5 (skip A4)      │      │
│                        ▼                                    ▼      │
│                                              ┌─────────────┐       │
│                                              │ A4 Promote  │       │
│                                              └──────┬──────┘       │
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

**Clearing the bar skips A3 entirely and goes straight to A4** (§5.0) — the first passing iteration is the last iteration, by rule, not by A3's judgment.

---

## 1A. Flow 0 — the nanoAQRL loop ★

**This is what actually runs first.** Everything in §2–§12 is the destination; this is the starting point (TRD §2A). One agent, one editable file, no queue, no orchestration.

```
      program.md  (human-written: goal, rules, acceptance bar)
            │
            ▼
   ┌──► agent edits strategy.py
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
   │   fail ├──────────────► discard, NO score computed
   │        │
   │   pass ▼
   │    rolling walk-forward (1yr test, purged + embargo, 2× costs)
   │    all folds CONCATENATED into one OOS series
   │        │
   │        ▼
   │    honest_score = SR_oos − 2·SE(SR) − SR*(N_trials)
   │        │
   │   ┌────┴──────────────┐
   │   │                   │
   │ improved?          worse/equal
   │   │                   │
   │   ▼                   ▼
   │ keep commit        git reset
   │   │                   │
   │   └─────────┬─────────┘
   │             ▼
   │   append row to results.tsv
   │   insert row into experiments (SQLite, with code_commit)
   │             │
   └─────────────┘   repeat, unattended, no human interruption
```

### 1A.1 Rules

- **`strategy.py` is the only writable file.** `data.py` is read-only; `evaluate.py` is neither readable nor writable.
- **The bar is enforced in `evaluate.py`, not merely stated in `program.md`** (TRD §4A.3b). `program.md` tells the agent what it is aiming at; `evaluate.py` decides whether it got there. Otherwise the agent grades its own homework.
- **One float drives the loop.** All other metrics are computed and stored, but only `honest_score` decides keep vs discard.
- **`program.md` is human-edited.** As the agent makes avoidable mistakes, the human adds a line. That file — not an agent-maintained knowledge base — is where accumulated wisdom lives in v1. Its required contents, including the anti-look-ahead rule set and the reveal/hide split, are specified in TRD §2A.3a.
- **Every run gets a status:** `keep` · `discard` · `crash`. No result goes unjudged.
- **Do not stop to ask the human.** Human gates exist only at paper trading and live capital.
- **Stop on satisficing, not maximising.** The first strategy clearing the pre-set bar wins (PRD §13.2). A later, higher score replaces it only by a wide margin on untouched data.

### 1A.2 What this flow deliberately omits

No A1/A3/A4/A5, no job queue, no scheduler, no knowledge graph, no research plans, no promotion committee. Each is added when a specific pain is felt — the loop plateaus, a question arrives that `results.tsv` cannot answer, one agent visibly fails at one job. Not on a schedule.

---

## 2. Flow 1 — Hypothesis Generation (A1)

### 2.0 A1 is stateless — there is no "since last time" ★

**A1 has no memory between runs.** Every `GENERATE_SPEC` job is a brand-new Claude session that knows nothing about any prior run (TRD §2A "Claude is stateless"). So the question "does it look at new knowledge or old knowledge" has a specific answer: **neither, exclusively — every run searches the whole combined pool, old and new mixed together, fresh, every time.** There is no separate "new" bucket A1 tracks. Freshly written `external_knowledge` rows simply become part of the same searchable pool the very next time anyone queries it.

**Trigger — three kinds, all producing the same job type:**
1. **Nightly batch** — one `GENERATE_SPEC` job per active `research_goal` with unused hypothesis budget.
2. **Curiosity closure** — a `research_question` gets answered by new external knowledge.
3. **Novelty push ★** — when the Librarian writes an `external_knowledge` row with `novelty_score` above a threshold, it enqueues a `GENERATE_SPEC` job directly instead of waiting for the nightly batch, so a genuinely new idea doesn't sit unused for a day.

### 2.1 The Research Brief — what A1 actually receives ★

The worker does **not** hand A1 the database. It hands A1 a small, targeted packet — the **Research Brief** — built by a **relevance search** (vector similarity + structured filters on market/timeframe/category), not a full read of every row. At scale this is the only thing that keeps a job cheap regardless of how large the knowledge base has grown.

```
Scheduler picks up GENERATE_SPEC job
        │
        ▼
Worker assembles the RESEARCH BRIEF (Python, not Claude):
        ├── the research_goal + its allocation bucket (70/20/10)
        ├── top-K relevant external_knowledge      ← candidates, untested (relevance search)
        ├── top-K relevant knowledge_entries + edges ← tested, trusted (relevance search)
        ├── open research_questions for this goal
        ├── recent failure_reasons in this family     (anti-amnesia)
        ├── operator catalog valid for this market/timeframe
        └── trials already spent in this family        (ties to the honest score's trial haircut, TRD §4A.2a)
        │
        ▼
Claude session (A1) — reads the brief, proposes ONE spec
```

**Combination happens in A1's reasoning, not in a database join.** The worker's only job is to make sure the *right* old (trusted) and new (candidate) items land in the same brief; noticing a useful combination between them is exactly what the LLM is for. Concretely:

```
OLD, trusted (knowledge_entries):
  "ATR multiplier > 3.0 overfits in this NSE trend family"

NEW, untested (external_knowledge, from the Librarian):
  "Volatility-normalized position sizing reduces drawdown in trending regimes"

→ A1's proposal: replace the fixed ATR multiplier with volatility-normalized
  sizing — same purpose, a mechanism that sidesteps the known failure mode,
  incorporating the new idea while respecting the old lesson.
```

### 2.2 A1's output — one row, always traceable back to both sources

```
Structured output (→ strategy_specs, Backend-Schema §3):
        hypothesis            — one falsifiable sentence
        rationale              — why this is worth trying
        entry_logic, exit_logic, filter_logic, risk_logic   — operator DAG
        universe                — instrument selection
        parameters              — names, defaults, allowed ranges
        expected_behavior      — A1's prediction, scored later for calibration
        source_external_knowledge_ids  — which candidate ideas it drew on
        source_internal_knowledge_ids  — which tested lessons it respected/avoided
        prompt_version
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

**Key property, unchanged:** A1 never sees a raw paper or raw market data. It sees pre-digested, structured records — from both knowledge bases — and its whole job is judgment about what to investigate next.

**Anti-amnesia check:** before proposing, the worker surfaces prior failures matching the proposed operators. If knowledge says "ATR > 3.0 always overfits here," A1 receives that and must justify contradicting it.

---

## 3. Flow 2 — Implementation (A2)

**Trigger:** the previous flow's write is the trigger. A `strategy_specs` insert (Flow 1) or an A3 `research_plans` insert (Flow 4) fires an event; the scheduler turns that event into an `IMPLEMENT` job. A2 never receives a call from A1 or A3 directly — it only ever picks up a job the scheduler queued because it noticed a row appear.

### 3.1 The Implementation Brief — what A2 receives

Same pattern as A1's Research Brief (§2.1): a focused packet, not the database.

```
Worker assembles the IMPLEMENTATION BRIEF:
        ├── strategy_spec (hypothesis, operator DAG, parameters, universe)
        ├── research_plan (if iteration ≥ 2) — WHAT to change, not HOW
        ├── previous code_version + diff history
        ├── previous evaluation report
        ├── operator implementations available (actual code, so it knows how to call each block)
        └── anti-look-ahead rules from program.md (TRD §2A.3a)
```

**A2 never receives `evaluate.py`.** It knows the correctness rules it must follow, but not how its score will be computed — same reasoning as everywhere else this boundary appears: an agent that can see the scorer eventually aims at the scorer instead of the market.

### 3.2 What A2 does — translation, not invention

It assembles operator-library building blocks into runnable code exactly as the spec's plain-language logic describes. It is not free to invent new logic:

```
entry_logic: "JMA slope turns positive AND ATR expands beyond 20-day average"
        ↓
def entry_signal(df):
    jma = jma_slope(df.close, period=14)
    atr_exp = atr_expansion(df, lookback=20)
    return (jma > 0) & atr_exp
```

### 3.3 A2's output

```
INSERT code_versions row (Backend-Schema §4):
        code_path, code_hash, git_commit
        diff_from_parent        — empty on iteration 1
        change_summary          — plain-language: what changed and why
        implements_plan_id      — which research_plan this responds to (null on iteration 1)
        compile_ok, static_check_results
        │
        ▼
Static checks (Python): compile, lint, look-ahead scan, leakage scan
        │
        ├── FAIL ──► enqueue FIX_CODE with diagnostics (bounded retries)
        │            a static-check failure is a BUG in the code, never a research finding
        │            after k failures → quarantine strategy for human inspection
        │
        └── PASS ──► Emit event → enqueue EVALUATE job
```

**Boundary:** A2 implements. It does not decide research direction. If A2 believes the plan is wrong, it records the objection in `change_summary` and implements anyway — the objection surfaces to A3, not acted on unilaterally.

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

**Trigger:** `REVIEW` job — but only when the evaluation just completed **failed the hard bar.** The worker checks `bar_result` the instant `evaluate.py` returns, *before* deciding whether to invoke Claude at all (App-Flow §5.0).

### 5.0 Clearing the bar is an immediate, unconditional stop ★

**The moment any iteration clears the acceptance bar, iteration on that strategy stops — permanently, right then.** Not "stop if it also fails to improve further." Not "try a few more times to see if it can do even better." The first pass is the last iteration. A3 is not even invoked for that iteration's stop/continue decision — the worker checks `bar_result` in Python and routes straight to `PROMOTE` before any Claude session is spent.

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
enqueue      enqueue REVIEW (A3) — this is Flow 4 proper, below
PROMOTE      │
(A4) —       ▼
skip A3   A3 decides: ITERATE, or give up (§5.1)
entirely
```

**Why this is a hard rule, not a judgment call:** this *is* satisficing (PRD §13.2) — take the first strategy that clears a pre-registered bar, never the best after many tries. Leaving "should I keep pushing for a higher score" as something A3 could decide would let an LLM quietly override the whole principle, one plausible-sounding justification at a time. So it isn't offered as a choice at all: the worker enforces it before Claude is ever asked.

**A consequence worth stating plainly: `acceptance_bars.min_score` already sets the floor.** Since the bar itself includes a minimum score requirement, clearing the bar already means "good enough by the standard set in advance." There is nothing left to optimize for on this strategy — continuing would only mean spending more of this family's trial budget and more of its irreplaceable out-of-sample data (TRD §4A.3c) chasing a number nobody asked for.

### 5.1 Below the bar — this is where A3 actually operates

Everything past this point in Flow 4 only ever happens **before** a strategy has cleared the bar. Once it clears, this flow is done — see §5.0.

```
Worker assembles context:
        ├── strategy_spec (the original hypothesis)
        ├── all prior experiments for this strategy (full history)
        ├── current evaluation report — bar_failed_on + the raw diagnostic metrics
        │      (no honest_score exists on a bar failure — see TRD §4A.3a)
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
   ├── PLATEAU  → 5 consecutive bar failures, never once cleared it (§5.1a)
   │              → enqueue ARCHIVE (A5) directly, skip A4
   │              (failure_reason = plateaued_below_bar)
   │
   └── REJECT   → hopeless before even reaching patience
                  → enqueue ARCHIVE (A5) directly, skip A4
```

**Note what's missing:** there is no `PROMOTE` verdict here anymore, and no path from this flow to A4. The only way to A4 is clearing the bar (§5.0). A3's job below the bar is narrower than it looks — decide whether to try again, or give up.

#### Stop conditions, checked by the worker before invoking Claude

| Condition | Action |
|---|---|
| **Bar cleared** | → A4 immediately, A3 not invoked for this decision (§5.0) |
| **5 consecutive bar failures, never cleared** | → PLATEAU → A5, `plateaued_below_bar` |
| Iteration/token/compute budget exhausted before ever clearing the bar | → A5, same reason |
| A3 judges further modification futile | → REJECT → A5 |
| Hard iteration cap (backstop, default ~20–25) | → forced PLATEAU → A5 |

### 5.1a Plateau, precisely — only ever a below-the-bar concept ★

Since clearing the bar is now an immediate stop (§5.0), there is never more than one *passing* evaluation for a given strategy — so there is nothing to compare passing scores against, and no notion of "improvement between passing attempts" is needed. Plateau counting is purely about the climb **toward** the bar:

```
Each iteration that FAILS the bar:
    consecutive_bar_failures += 1

Each iteration that CLEARS the bar:
    → immediate PROMOTE (§5.0). Counting stops; it never reaches here again.

5 consecutive bar failures, without ever clearing it once → PLATEAU
```

A bar-failing evaluation has **no `honest_score`** to compare (TRD §4A.3a — the score is only computed after the bar passes), so A3 reasons over the raw diagnostic instead: `bar_failed_on`, and the underlying metrics that fed that check (how far below `min_trades`, how far over `max_drawdown`, etc.) — the same data already stored on every `evaluations` row regardless of pass/fail. That's enough for A3 to judge "getting closer" from "stuck" without needing a synthetic score for failing attempts.

**PLATEAU always routes to A5, never to A4.** The earlier two-destination version of this rule (§5.1a, prior revision) assumed a plateau could happen *after* clearing the bar — that possibility no longer exists once §5.0 is the rule, so the routing collapses to one destination.

**The count is configurable per campaign** (`acceptance_bars.plateau_patience`, Backend-Schema §15) — 5 is the default. The hard iteration cap remains as an outer backstop in case something dodges this logic.

### 5.2 The research plan is not code

A3 says *"replace the fixed stop with an ATR trailing stop because exits are cutting winners in trending regimes."* It does not write the function. This keeps the scientist/engineer boundary intact and makes A3's reasoning reviewable in plain language.

---

## 6. Flow 5 — Promotion (A4)

**Trigger:** `PROMOTE` job — fired the instant an evaluation clears the bar (§5.0). A3 is not necessarily involved in getting here at all; A4 reads the winning evaluation directly.

```
Worker assembles the ENTIRE research history:
        ├── original hypothesis
        ├── every iteration and what changed
        ├── every evaluation report (all the bar-failing attempts too, not just the winner)
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

**A4 must weigh iteration count.** A strategy that cleared the bar on iteration 31 of 47 tries is a fundamentally different object from one that cleared it on try 2. More attempts = more multiple testing = higher overfitting risk, and A4 sees that explicitly.

**A4 does not check correlation with the existing portfolio.** ★ That question — "is this a genuinely new source of profit, or the same bet you already have?" — is portfolio-construction work, and multi-strategy portfolio construction is explicitly out of scope for v1 (PRD §3, Implementation_Plan §18). A4 judges a strategy **on its own merits only**: is it real, is it tradeable, does the evidence hold up. Portfolio fit is a separate, later capability — see the note in Flow 7 (§8) for where correlation still shows up, and why that's different from A4 assessing it.

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
         ★ computed fresh by plain Python from stored return series —
           NOT part of A4's brief or decision (App-Flow §6). A4 judges
           the strategy alone; the human gets this as extra context A4
           never saw, matching the "give the human more than the agent
           used" principle (UI-UX-Brief §1.1)
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

## 11. Flow 10 — External Knowledge Ingestion (the Librarian Agent, no LLM until the extraction step)

**One unified pipeline for every source type.** A GitHub source contributes text (README, docs, comments) through the exact same path as a paper or a blog post — no separate tooling branch for code.

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
        Survivors only → THE LIBRARIAN (PRD §6.2)
                      │
              ┌───────┴────────┐
              │  big document?  │
              └───────┬────────┘
                  yes  │  no
                       ▼
          chunk by structure (section/heading — never
          a blind token window, TRD §7.2a)
                       │
                       ▼
          PASS 1 — per chunk: what claim/method is here?
          → document_chunks.chunk_extraction
                       │
                       ▼
          PASS 2 — synthesize across all chunks of this
          document into a small number of DISTINCT ideas
          (typically 2-3 per paper, never one blob)
                       │
                       ▼
          classify + tag extraction_confidence
          + evidence_tier = 'external_claim' (TRD §7.2b)
                       │
                       ▼
        One external_knowledge row PER IDEA, each carrying
        source_chunk_ids back to the exact passage
                       │
                       ▼
        Available to A1 — as a candidate to test, not a fact
```

**Claude never crawls.** Python collectors do the fetching. A document is read by the Librarian exactly once in its lifetime, then never again — subsequent access, by A1 or anyone else, is to the structured `external_knowledge` rows.

**Targeted mode:** collectors also consume the `research_questions` queue, so searches are driven by the lab's own gaps rather than only broad topical sweeps.

**The Librarian runs outside the five-agent loop.** It is never invoked by A3 or A4, and it never blocks an experiment — it only ever adds candidates to the shelf that A1 reads from next cycle.

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

## 15. Flow 12 — Null-World Calibration (runs before anything real)

The procedure that establishes whether the pipeline can be trusted at all (PRD §4.5, TRD §8A.3).

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
      │                       │
   0 found                 12 found
      │                       │
      ▼                       ▼
 pipeline_trusted        pipeline_suspect
 proceed to real data    fix evaluate.py / scoring rule,
                         then re-run. Do NOT proceed.
            │
            ▼
Record null_world_runs · surface FDR on the Laboratory screen
Record max_score_observed → the bar any real result must clear
```

**Re-run as a regression test** after every change to `evaluate.py`, the scoring rule, or any profile. Throughput is tied to the result (the autonomy ratchet, TRD §8A.4): if FDR rises, experiments-per-day automatically fall.

---

## 16. Flow 13 — Vault Access (the only path to real capital)

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
      │            │
  budget = 0   budget > 0
      │            │
      ▼            ▼
  BLOCKED      open the vault segment  ← logged, decrements budget
  until new         │
  data exists       ▼
             score on data the loop never touched
                    │
              ┌─────┴──────┐
              │            │
          confirmed    contradicted
              │            │
              ▼            ▼
        human gate    reject; record as knowledge;
                      family budget already spent
```

**The loop has no read path to the vault** — not "must not," *cannot*. Every other protection depends on honestly counting trials, which becomes unknowable once hypotheses are influenced by memory of past results. This is the one defence that does not depend on counting anything.

---

## 17. Open Flow Questions

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
| 2026-07-27 | Added §1A Flow 0 (the nanoAQRL loop that actually runs first), §15 null-world calibration, §16 vault access. |
| 2026-07-28 | Added **§5.1a — the plateau rule, precisely.** Default 5 consecutive non-improving iterations; "improving" defined against a noise margin (`0.5 × se_sr`) rather than raw score comparison; a bar failure counts as non-improvement. Plateau now routes to A4 (candidate, best-so-far cleared the bar) or straight to A5 (never cleared it, `failure_reason = plateaued_below_bar`) instead of a single ambiguous destination. Patience, margin factor, and the hard cap are now configurable per campaign via `acceptance_bars`. |
| 2026-07-27 | Flow 0 now points at TRD §2A.3a for the required contents of `program.md`. |
| 2026-07-27 | Rewrote **Flow 2** with the same treatment as Flow 1: named the **Implementation Brief**, made explicit that a job only ever begins because the scheduler noticed a database row (never a direct call from A1 or A3), stated plainly that A2 never receives `evaluate.py`, and pulled the `code_versions` output into an explicit schema block. |
| 2026-07-27 | Rewrote **Flow 1** around the **Research Brief**: made explicit that A1 is stateless and performs a fresh relevance search over the whole combined knowledge pool on every run rather than tracking "new vs old"; added the high-novelty push trigger so a standout new idea doesn't wait for the nightly batch; clarified that combining external (candidate) and internal (tested) knowledge happens in A1's own reasoning, not a database join, with a worked example; split A1's output traceability into `source_external_knowledge_ids` and `source_internal_knowledge_ids`. |
| 2026-07-27 | Rewrote **Flow 10** around the Librarian Agent: a single unified pipeline for every source type (no separate code-repository branch), explicit chunk → per-chunk extraction → cross-chunk synthesis → classify steps, and confirmation that the Librarian sits outside the five-agent loop and never blocks an experiment. |
| 2026-07-27 | Flow 0 updated for the resolved honest score — the hard bar now gates inside `evaluate.py` before any score is computed, and one float drives keep/discard. |
