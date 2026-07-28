# UI/UX Brief — AQRL Dashboard

> **Status:** Living document. Updated after every design session.
> **Last updated:** 2026-07-27
> **Companion docs:** `PRD.md` (gates & criteria), `App-Flow.md` (what the human sees and when)

---

## 0. What v1 actually looks like

**There is no dashboard in v1.** The nanoAQRL loop (TRD §2A) produces `results.tsv` — one row per experiment, read in a terminal — and a `aqrl review` CLI for the human gates (Stage 9).

That is deliberate and sufficient. Reading every row by hand for the first few weeks is how you learn what the agent actually does, and it is the input that improves `program.md`. A dashboard built before the loop produces candidates worth reviewing is decoration.

Everything below describes the destination, built at Stage 12 — **except §7a Observability**, which ships far earlier. Decisions need candidates to decide on; watching the machine work is needed the moment more than one agent can be running at once. See §7a and `Implementation_Plan.md` Stage 4.

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
- ❌ Not a notebook. No code editing.
- ❌ The **decision** screens (§3–§6) are not a data explorer — deep ad-hoc analysis lives in §7a Observability, kept structurally separate from where capital decisions are made.
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
├── ⬤ Health              ← live & paper deployments, ranked by concern (flat, ignores hierarchy)
│     └── Deployment detail
│
├── ⬤ Pipeline            ← the LIFECYCLE view — everything, and where it sits (§7)
│     ├── Research → Review → Paper → Review → Live 1–5% → Live scaled → Retired
│     ├── Group by: Strategy (default) | Market — a toggle, not a fixed tree (§7.1)
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
└── ⬤ System              ← jobs, budgets, errors, live agent activity, data explorer (§7a)
```

**Landing page is Decisions.** If there are none, it says so plainly and shows the single most concerning health item. Empty state is a feature, not a gap.

**Paper and live are not top-level tabs.** They are stages on one lifecycle track, not two separate rooms — a promotion is a strategy moving along a line, not switching apps. Splitting them also hides the single most useful comparison in the whole product: how live performance decayed relative to paper, which is what calibrates the pipeline itself (§7.2).

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
- Correlation with existing live strategies — ★ computed independently by the dashboard backend, not sourced from A4. A4 judges the strategy alone (App-Flow §6); this is deliberately *more* than A4 itself saw
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

**Flat and ranked by concern, not by return, and it deliberately ignores the Pipeline screen's strategy/market grouping (§7.2).** A health concern can occur in paper or live, under any strategy, in any market — burying it in a tree is how you miss the one thing you needed to see first. A strategy making money with degrading statistical behavior ranks above a healthy one in a normal drawdown.

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

- **Null-world false discovery rate** ★ — the headline integrity number (PRD §4.5). How many "discoveries" the pipeline reports when run on data containing no alpha by construction. Displayed beside cost-per-discovery, with the date of the last calibration run and the `max_score_observed` in noise — the bar any real result must clear. If this rises, **nothing else on this screen means anything.**
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

## 7. Screen: Pipeline

**The atomic unit is the deployment — `strategy × market × mode`**, not the strategy and not the market alone. A deployment is the thing that actually has trades, health, P&L and a lifecycle stage; grouping is a *view* over deployments, not a fixed tree.

### 7.1 Lifecycle track

The primary layout is the stage the deployment sits at, not paper-vs-live:

```
Research → Awaiting review → Paper → Awaiting review → Live 1–5% → Live scaled → Retired
```

Strategies in active research show their current iteration and score trajectory; recently rejected strategies show structured reasons; **quarantined** strategies need human debugging (the only place the human does technical work in the whole product).

### 7.2 Grouping toggle — strategy vs market

Below the lifecycle track, deployments are grouped one of two ways. **Neither is "correct" — they answer different questions, so this is a toggle, not a decision to commit to.**

**By strategy (default)** — how research and knowledge accrue: "JMA works in commodities, weak in forex" is a strategy-first statement.

```
JMA + ATR Trend   (14 trials across 3 markets — see §7.3)
  ├── NSE Equity     LIVE 4%    🟢
  ├── Commodities    PAPER      🟡
  └── Crypto         PAPER      🔴
```

**By market** — the risk view. Everything in one market moves together in a crash, so aggregate exposure per market is what gets checked when things get ugly.

```
NSE Equity
  ├── JMA + ATR Trend       LIVE 4%
  ├── Mean Reversion        PAPER
  └── Vol Breakout          LIVE 2%
```

### 7.3 Family trial count on group headers ★

When grouped by strategy, the header shows the **family trial count**, not just the strategy name.

The same strategy tried across 3 markets is not "one strategy, three markets" — it is **three trials of the same idea**, and the deflated Sharpe reads the family count exactly this way (TRD §4A.2a, Backend-Schema `strategies.family`). The dashboard must reinforce that reading, not quietly undermine it: showing "JMA+ATR (14 trials across 3 markets)" on the group header keeps the honest framing visible at the exact moment a human is deciding whether the one good result is real or just the survivor.

---

## 7a. Observability — ships early, not at Stage 12 ★

**This is not the decision layer. It exists to debug the machine, not to approve capital.** Once a scheduler is dispatching jobs to more than one agent, grepping SQLite from a terminal to find out why something is stuck becomes real pain — so this ships as soon as the job queue exists (Implementation_Plan Stage 4–5), independent of the curated Decisions/Health/Laboratory/Knowledge screens that wait for Stage 12.

nanoAQRL (Stage 0) does not need this: one agent, sequential, and `results.tsv` already answers everything. Observability earns its place only once there is concurrency to watch.

Lives under **System**, as two sub-views.

### 7a.1 Activity feed — which agent is doing what, right now

A live job feed sourced directly from the `jobs` table:

```
🟢 A2  Quant Engineer    experiment #4821   implementing...        12s
🟢 A3  Research Reviewer experiment #4819   reviewing...            3s
⚪ A1  Research Scientist   —                idle
✅ A4  Promotion         experiment #4802   done   (47s, $0.08)
❌ A2  Quant Engineer    experiment #4818   FAILED — compile error
```

Every row is agent, target strategy/experiment, status, duration, and cost — and every row deep-links into the data explorer below (§7a.2), so "why is this stuck" is one click from "what actually happened."

### 7a.2 Data explorer — opening the database without opening a terminal

A read-only browser over the SQLite tables: a table list, a row viewer with filter and sort, and rows that link to each other (a job row links to its experiment, which links to its evaluation) instead of the human writing joins by hand.

Because this is local, single-user, and strictly read-only, it also includes a **raw SQL query box** — there is no audience of more than one to protect against, so a query builder would be wasted effort. No write path exists anywhere in this view, ever.

### 7a.3 The one deliberate exception to "no polling"

`UI-UX-Brief.md` §11 sets manual-refresh-only as the default, specifically so the product stays calm rather than casino-like. **The activity feed is the one exception.** A live "what's running" view that is twenty seconds stale defeats its own purpose. Every other screen — including the data explorer — stays manual-refresh; only §7a.1 auto-refreshes.

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
| Refresh | Manual + on-navigation, everywhere **except the Activity feed (§7a.1)**, which auto-refreshes — the one named exception to no-polling |

**The decision layer is the last thing built, not the first.** A CLI that lists pending decisions is sufficient to validate the loop; the curated Decisions/Health/Laboratory/Knowledge screens come once the pipeline reliably produces candidates worth reviewing. **Observability (§7a) ships much earlier** — as soon as multiple agents can be running concurrently, because at that point a terminal alone is no longer enough to tell what the machine is doing.

---

## 12. Open UX Questions

- [ ] Should the review screen hide A4's recommendation until the human has read the evidence, to avoid anchoring?
- [x] ~~How is a multi-strategy portfolio view presented once several strategies are live simultaneously?~~ — **resolved: the Pipeline screen's strategy/market grouping toggle (§7.2)**
- [ ] Mobile: read-only health monitoring, or approvals too? (Leaning read-only — capital decisions deserve a full screen.)
- [ ] How much of the 47-iteration history is shown by default before it becomes noise?
- [ ] Should rejected strategies remain browsable indefinitely, or be archived out of the main views after N days?
- [ ] Does the data explorer (§7a.2) need row-level access control before it is ever exposed beyond localhost, given the raw SQL box?
- [ ] Retention on the Activity feed (§7a.1) — how far back does job history stay live-browsable before it rolls into the plain experiment tables?

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial brief. "Make it easy to reject" premise, case-against-first review layout, health-not-profit color semantics, laboratory self-measurement screen, sparse notification policy. |
| 2026-07-27 | Added §0 — v1 has no dashboard; `results.tsv` plus a review CLI is the interface until Stage 12. Added the null-world false discovery rate panel to the Laboratory screen. |
| 2026-07-27 | Restructured navigation: paper/live are no longer top-level tabs — replaced with a single lifecycle-track Pipeline screen (§7) using deployment (`strategy×market×mode`) as the atomic unit and a strategy/market grouping toggle, with family trial count surfaced on group headers (§7.3) so cross-market re-runs of one idea are never mistaken for independent discoveries. Health stays flat and concern-ranked, deliberately ignoring the new grouping. Added **§7a Observability** — a live agent activity feed and a read-only data explorer with a raw SQL box, both shipped as soon as the job queue exists rather than waiting for Stage 12, with the activity feed carved out as the one named exception to the no-polling rule. |
| 2026-07-28 | Clarified that the review screen's portfolio-correlation item is computed independently by the dashboard, not sourced from A4 — A4 no longer assesses portfolio fit (App-Flow §6). |
