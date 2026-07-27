# UI/UX Brief — AQRL Dashboard

> **Status:** Living document. Updated after every design session.
> **Last updated:** 2026-07-27
> **Companion docs:** `PRD.md` (gates & criteria), `App-Flow.md` (what the human sees and when)

---

## 1. Design Premise

The dashboard is **not a control panel for running the lab.** The lab runs itself. The dashboard exists for three jobs only:

1. **Decide** — the two mandatory human gates (research→paper, paper→live)
2. **Detect** — surface strategies whose health is degrading
3. **Understand** — let the Research Director see whether the *laboratory itself* is improving

> If the human is doing routine work in this UI, the architecture has failed.

### 1.1 The core UX problem

The human will review a strategy that an AI iterated on 47 times, evaluated across 9 statistical tests, and recommended with 82% confidence. The human has maybe **10 minutes** and must not rubber-stamp it.

So the entire interface is designed around one question: **make it easy to reject.** Approval should require the evidence to be genuinely convincing, and the UI should make weak evidence *visually obvious* rather than burying it in a metrics table.

### 1.2 Anti-goals

- ❌ Not a trading terminal. No order entry, no live P&L ticker, no charts-for-the-sake-of-charts.
- ❌ Not a notebook. No code editing, no ad-hoc query building.
- ❌ Not a data explorer. Deep analysis happens in DuckDB/notebooks; this surfaces decisions.
- ❌ No dark-pattern nudging toward approval. Default state of every gate is **not approved**.

---

## 2. Information Architecture

```
AQRL Dashboard
│
├── ⬤ Decisions            ← the only page that ever demands attention
│     ├── Pending: Research → Paper
│     ├── Pending: Paper → Live
│     └── Pending: Lifecycle actions (reduce / pause / retire)
│
├── ⬤ Health              ← live & paper deployments, ranked by concern
│     └── Deployment detail
│
├── ⬤ Pipeline            ← the funnel, at a glance
│     ├── Strategies in research
│     ├── Recently rejected (with reasons)
│     └── Quarantined (needs human debugging)
│
├── ⬤ Laboratory          ← is the LAB improving?
│     ├── Funnel metrics
│     ├── Cost per credible discovery
│     └── Repeat-failure rate
│
├── ⬤ Knowledge           ← browsable memory
│     ├── Lessons & global rules
│     ├── Knowledge graph
│     └── Research questions (curiosity queue)
│
└── ⬤ System              ← jobs, budgets, errors
```

**Landing page is Decisions.** If there are none, it says so plainly and shows the single most concerning health item. Empty state is a feature, not a gap.

---

## 3. Screen: Decisions (the most important screen)

### 3.1 Queue view

A ranked list, not a grid. Each row:

```
┌────────────────────────────────────────────────────────────┐
│ 🔬 JMA slope + ATR expansion breakout                      │
│    NSE Equity · 15min · Swing                              │
│                                                            │
│    Sharpe 1.62   DD -18.4%   PBO 0.21   DSR 0.94          │
│    ⚠ 47 iterations                                        │
│                                                            │
│    A4 recommends: APPROVE (confidence 0.82)                │
│    Waiting 3 days                          [ Review → ]    │
└────────────────────────────────────────────────────────────┘
```

**Iteration count is displayed as a warning badge, not a neutral stat.** It is the single best proxy for overfitting risk that a human can eyeball in one second.

Sort by: recommendation strength × waiting time. Aging items grow more prominent — nothing silently rots in the queue.

### 3.2 Review view — the decision page

Five stacked sections, in this order. The order is deliberate: **the story first, the doubts second, the numbers third.**

**① The claim, in one sentence**
> *"JMA slope steepening combined with ATR expansion predicts multi-day breakouts in NSE mid-caps."*

Plain language, no jargon, no metrics. If the human can't understand the hypothesis, they should reject on that basis alone.

**② The case against it** — placed *above* the case for it

A dedicated red-bordered panel:
- Iteration count and what it implies about multiple testing
- Which tests came closest to failing (with margin)
- Which regimes it performed worst in
- Cost breakeven multiplier ("edge disappears at 2.3× assumed costs")
- Correlation with existing live strategies
- Any contradicting knowledge entries ("we have 3 prior lessons saying ATR > 3.0 overfits in this family; this uses 2.8")

This inversion is the single most important UI decision in the product. Standard dashboards lead with green metrics and bury caveats. **We lead with the reasons to say no.**

**③ Evidence** — the robustness battery

A compact test matrix, not a wall of numbers:

```
Walk-forward       ████████░░  8/10 windows      PASS
Deflated Sharpe    0.94 (156 trials)             PASS
PBO                0.21  (threshold 0.35)        PASS
White's RC         p=0.03                        PASS
Monte Carlo P5     +4.2% CAGR                    PASS
Regime consistency ███████░░░  weak in high-vol  WARN
Cost sensitivity   breakeven 2.3×                WARN
Parameter stability ████████░░                   PASS
```

Each row expands to show method, threshold, and reasoning. **Every threshold is visible** — a passing test with an invisible threshold is not evidence.

**④ Charts** — exactly four, no more

- Equity curve with out-of-sample and walk-forward windows shaded distinctly
- Underwater (drawdown) curve
- Return distribution vs Monte Carlo envelope
- Performance by regime (small multiples)

In-sample and out-of-sample must be *visually unmistakable* — different treatment, not just a legend entry.

**⑤ The research history** — collapsed by default

A timeline of all 47 iterations: what changed, what happened to the score. Expandable to read A3's diagnosis at each step. This is where a human catches "it only started working after we added the third filter" — the classic overfitting tell.

### 3.3 The decision control

```
                [ Reject ]  [ Request more research ]  [ Approve → Paper ]
```

- No pre-selected default.
- **Approve requires a typed note.** A one-line justification, recorded in `promotions.human_notes`. This is friction on purpose — it forces the human to articulate why, and it becomes training data for evaluating A4's calibration later.
- Reject also requires a reason, chosen from the structured failure categories plus free text. Rejection reasons flow to A5 and become knowledge.

---

## 4. Screen: Health

### 4.1 Deployment list

Ranked by **concern, not by return.** A strategy making money with degrading statistical behavior ranks above a healthy one in a normal drawdown.

```
🔴  Momentum Breakout v3     LIVE 4%    -12.4%    Behavior diverged — sharpe z=-2.8
🟠  JMA Trend NSE            LIVE 2%     +3.1%    Slippage 2.1× expected
🟡  Mean Reversion Crypto    PAPER       -4.2%    Below expected win rate
🟢  ATR Channel Commodities  LIVE 5%     +8.7%    Healthy
🟢  Vol Target Forex         PAPER      +11.2%    Healthy · 187/250 trades
```

The status color communicates **"is it behaving like what we validated?"** — never simply "is it up or down."

### 4.2 Deployment detail — the health question

Header states the verdict in words before any number:

> **🟢 Behaving as validated.** Currently in a sideways regime where this strategy historically underperforms. The −6% drawdown is within the expected range for these conditions.

Then:

- **Live vs Expected** — paired bars for Sharpe, win rate, avg trade, max DD, with the validated confidence band shaded. Deviation shown as a z-score, because "1.2 vs 1.6" means nothing without knowing the expected spread.
- **Regime context panel** — current regime, and whether the strategy historically struggles there. This one panel prevents the most common bad decision: killing a healthy strategy for an expected drawdown.
- **Execution quality** — expected vs actual slippage, missed fills, liquidity.
- **Promotion progress** (paper only) — trade count against requirement, regime coverage checklist.
- **Lifecycle timeline** — every event, who triggered it, why.

### 4.3 Kill switch

Always reachable, always confirmed with a typed strategy name. Never behind a menu. Its presence is reassuring; its ease of use must not be.

---

## 5. Screen: Laboratory (is the lab itself improving?)

This is the screen the Research Director actually cares about long-term. It answers: **is this thing getting smarter, or just busier?**

```
┌─ Funnel (last 30 days) ─────────────────────────────┐
│                                                     │
│  Hypotheses      2,847  ████████████████████████    │
│  Implemented     2,610  ██████████████████████      │
│  Passed P0       1,982  █████████████████           │
│  Passed P1         604  █████                       │
│  Passed P2         187  ██                          │
│  Passed P3          11  ▏                           │
│  → Human review      3  ▏                           │
│  → Paper             1  ▏                           │
│                                                     │
│  Yield: 0.035%          Cost/discovery: $412        │
└─────────────────────────────────────────────────────┘
```

Additional panels:

- **Repeat-failure rate** — how often did we test something memory should have killed? **Target → 0.** If this rises, A5 is not working and the whole premise is broken.
- **Reproducibility rate** — target 100%. Any deviation is an integrity emergency, displayed as such.
- **Knowledge growth** — new lessons and graph edges per week, split novel vs reinforcing.
- **Survival curve** — of strategies that reached live, how many remain healthy at 3/6/12 months. The only chart that reflects real-world truth.
- **Agent calibration** — A1 predicted expected behavior; A3 predicted expected effect; A4 gave a confidence. How often were they right? An overconfident agent is a fixable problem, but only if measured.

---

## 6. Screen: Knowledge

Browsable memory. Three views:

**Lessons & rules** — searchable list of `knowledge_entries`, filterable by scope, market, confidence. Each shows evidence count **and counter-evidence count** side by side. A lesson with contradicting evidence must look visibly less certain.

**Knowledge graph** — interactive node-link view:
```
        ┌─ works in ──► Trending
Momentum┤
        └─ fails in ──► High Volatility  (14 experiments, confidence 0.87)
```
Edge thickness = evidence count. Clicking an edge lists the supporting experiments. **Every edge is traceable to experiments** — an unbacked edge is a bug, and the UI should make that visible rather than hide it.

**Research questions** — the curiosity queue. Open questions, what triggered them, whether they ever produced a hypothesis. This view directly answers "is the curiosity loop actually closing?"

---

## 7. Screen: Pipeline & System

**Pipeline** — the funnel as a live board: strategies in research with their current iteration and score trajectory; recently rejected with structured reasons; **quarantined** strategies that need human debugging (the only place the human does technical work).

**System** — job queue depth, running jobs, failure rates by class, budget consumption vs caps, scheduler heartbeat. Deliberately utilitarian. Budget exhaustion is displayed as a *normal state*, not an error.

---

## 8. Visual Language

### 8.1 Principles

| Principle | Application |
|---|---|
| **Evidence over aesthetics** | Every number carries its threshold or confidence interval |
| **Doubt is visible** | Warnings are not collapsed by default; the case-against precedes the case-for |
| **Comparison over absolutes** | Live metrics always paired with their validated expectation |
| **Traceability is one click** | Any claim expands to the evidence behind it |
| **Calm by default** | No animation, no auto-refresh flicker. This is a laboratory, not a casino |

### 8.2 Color semantics — fixed, never decorative

| Color | Meaning | Used for |
|---|---|---|
| Green | Behaving as validated | Health only — **never** "profitable" |
| Yellow | Deteriorating, still plausible | Health, warnings |
| Orange | Multiple warning signals | Health, pending action |
| Red | Diverged from validated model | Health, hard failures, kill switch |
| Neutral gray | Informational | Everything else |

Profit and loss use a **separate, non-red/green scale** to avoid collision with health semantics. This is deliberate: a losing strategy is not a red strategy, and conflating the two is exactly the confusion the whole health framework exists to prevent.

### 8.3 Charts

Follows the project's data-visualization standards. Chart-specific requirements:

- In-sample vs out-of-sample must be distinguishable **without** reading a legend
- Monte Carlo envelopes as shaded bands, actual path overlaid
- Every threshold drawn as an explicit reference line
- No dual axes, no 3D, no pie charts
- Every chart works in light and dark themes
- Wide tables and charts scroll within their own container; the page never scrolls horizontally

### 8.4 Typography & density

Information-dense but not cramped. Metrics in tabular figures so columns align. The review screen is the one place where generous whitespace matters — the human is making a capital-allocation decision and should not feel rushed.

---

## 9. Interaction Rules

1. **No destructive action without typed confirmation.** Kill switch, retire, reject-with-prejudice.
2. **No approval without a written note.** Friction is the point.
3. **Nothing auto-promotes.** Ever. There is no timeout that advances a strategy.
4. **Every screen is read-only except the decision controls.** The dashboard cannot edit strategies, code, or knowledge.
5. **Keyboard-first for the queue.** Reviewing many candidates should not require a mouse.
6. **Deep links everywhere.** Any experiment, lesson, or deployment is addressable by URL for sharing and returning.

---

## 10. Notifications

Deliberately sparse. The system should be quiet enough that a notification means something.

| Event | Channel |
|---|---|
| New promotion candidate | Dashboard badge + daily digest |
| Health → 🟠 orange | Dashboard + immediate push |
| Health → 🔴 red | Dashboard + immediate push (auto-halt already executed) |
| Kill switch fired | Immediate push |
| Scheduler down > 30 min | Immediate push |
| Budget exhausted | Daily digest only |
| Weekly lab report | Email |

**A novel discovery is not urgent.** A degrading live strategy is.

---

## 11. Technology

| Concern | Choice |
|---|---|
| v1 | Local web app, read-only against SQLite + Parquet/DuckDB |
| Rendering | Server-rendered pages; minimal client JS |
| Charts | Static/lightweight — no heavy dashboarding framework |
| Auth | None in v1 (localhost); added when hosted |
| Refresh | Manual + on-navigation. No polling |

The dashboard is the **last thing built**, not the first. A CLI that lists pending decisions is sufficient to validate the loop; the UI comes once the pipeline reliably produces candidates worth reviewing.

---

## 12. Open UX Questions

- [ ] Should the review screen hide A4's recommendation until the human has read the evidence, to avoid anchoring?
- [ ] How is a multi-strategy portfolio view presented once several strategies are live simultaneously?
- [ ] Mobile: read-only health monitoring, or approvals too? (Leaning read-only — capital decisions deserve a full screen.)
- [ ] How much of the 47-iteration history is shown by default before it becomes noise?
- [ ] Should rejected strategies remain browsable indefinitely, or be archived out of the main views after N days?

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial brief. "Make it easy to reject" premise, case-against-first review layout, health-not-profit color semantics, laboratory self-measurement screen, sparse notification policy. |
