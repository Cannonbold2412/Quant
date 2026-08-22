# UI/UX Brief — AQRL

> **Status:** Design complete for v1, and Stage 12 (the dashboard this brief specifies) is built — see Implementation_Plan.md §15 for the full build record and known limits. Stages 0-12 built overall; Stage 4a and Stage 13 remain design only.
> **Last updated:** 2026-08-05
> **Companion docs:** `PRD.md` (gates & criteria) · `App-Flow.md` (what the human sees and when) · `Backend-Schema.md` (what backs each view)

---

## 0. What Ships When

**There is no dashboard in v1.** The nanoAQRL loop (TRD §2) produces `results.tsv` — one row per experiment, read in a terminal — plus an `aqrl review` CLI for the human gates (Implementation_Plan Stage 9).

That is deliberate and sufficient. Reading every row by hand for the first few weeks is how you learn what the agent actually does, and it is the input that improves `program.md`.

Everything below is built in two separate waves, and the split matters:

| Wave | Contents | When | Purpose |
|---|---|---|---|
| **Observability** (§8) | Live agent activity feed, read-only data explorer | **Stage 4a** — as soon as the job queue exists | **Debug the machine.** Once a scheduler dispatches to more than one agent, a terminal stops answering "why is this stuck?" |
| **Decision layer** (§3–§7) | Decisions, Health, Pipeline, Laboratory, Knowledge | **Stage 12** — once the pipeline produces candidates worth reviewing | **Approve or reject capital.** Building it earlier is decorating a lab that cannot yet run an experiment |

---

## 1. Design Premise

The dashboard is **not a control panel for running the lab.** The lab runs itself. It exists for three jobs only:

1. **Decide** — the two mandatory human gates (research→paper, paper→live)
2. **Detect** — surface strategies whose health is degrading
3. **Understand** — let the Research Director see whether the *laboratory itself* is improving

> If the human is doing routine work in this UI, the architecture has failed.

### 1.1 The core UX problem

The human will review a strategy that an AI iterated on 47 times, evaluated across 9 statistical tests, and recommended with 82% confidence. **The human has maybe 10 minutes and must not rubber-stamp it.**

So the entire interface is designed around one goal: **make it easy to reject.** Approval should require the evidence to be genuinely convincing, and weak evidence must be *visually obvious* rather than buried in a metrics table.

A corollary that shapes several screens: **give the human more than the agent used.** Portfolio correlation, for instance, is deliberately computed by the dashboard and shown at review time even though A4 never saw it (App-Flow §7.2).

### 1.2 Anti-goals

- ❌ Not a trading terminal. No order entry, no live P&L ticker, no charts for the sake of charts.
- ❌ Not a notebook. No code editing.
- ❌ The **decision screens** (§3–§7) are not a data explorer — deep ad-hoc analysis lives in §8, kept structurally separate from where capital decisions are made.
- ❌ No dark-pattern nudging toward approval. **The default state of every gate is "not approved."**

---

## 2. Information Architecture

```
AQRL
│
├── ⬤ Decisions      ← the only page that ever demands attention
│     ├── Pending: Research → Paper
│     ├── Pending: Paper → Live
│     └── Pending: Lifecycle actions (reduce / pause / retire)
│
├── ⬤ Health        ← live & paper deployments, FLAT, ranked by concern
│     └── Deployment detail
│
├── ⬤ Pipeline      ← the lifecycle view: everything, and where it sits
│     ├── Research → Review → Paper → Review → Live 1–5% → Live scaled → Retired
│     ├── Group by: Strategy (default) | Market — a toggle, not a fixed tree
│     ├── Data-quality queue — unresolved flags block their snapshot
│     ├── Recently rejected, with structured reasons
│     └── Quarantined (needs human debugging)
│
├── ⬤ Laboratory    ← is the LAB improving?
│
├── ⬤ Knowledge     ← browsable memory, both trust tiers
│
└── ⬤ System        ← jobs, budgets, errors + Observability (§8)
```

**Landing page is Decisions.** If there are none, it says so plainly and shows the single most concerning health item. **Empty state is a feature, not a gap.**

### 2.1 Paper and live are not top-level tabs ★

They are **stages on one lifecycle track**, not two separate rooms. Three reasons:

1. A promotion is a strategy *moving along a line*, not switching apps.
2. Splitting them hides the single most useful comparison in the product: **how live performance decayed relative to paper**, which is what calibrates the pipeline itself.
3. Both lists are nearly empty for a long time. Splitting three deployments into two tabs gives two sad screens.

### 2.2 The atomic unit is the deployment

**`strategy × market × mode`** — that is the thing with trades, health, P&L and a lifecycle stage. Grouping is a *view over deployments*, never a fixed tree.

---

## 3. Screen: Decisions

> ✅ **Built.** The Decisions screen ships live at `aqrl dashboard serve`, displaying pending promotions (Research → Paper and Paper → Live), with all five stacked sections per §3.2 in the specified order. The decision control implements exactly the three buttons shown: Reject, Request more research (Defer), and Approve, routing through `gates.defer` (new 0010_promotion_defer.sql migration) and `gates.approve`. Rejection reasons flow to A5; approval requires a typed note recorded to `promotions.human_notes`.

### 3.1 Queue view

A ranked list, not a grid:

```
┌────────────────────────────────────────────────────────────┐
│ 🔬 JMA slope + ATR expansion breakout                      │
│    NSE Equity · 15min · Swing                              │
│                                                            │
│    Score 0.62   DD -18.4%   PBO 0.21   Trades 340         │
│    ⚠ cleared on iteration 31 of 47                        │
│                                                            │
│    A4 recommends: APPROVE (confidence 0.82)                │
│    Waiting 3 days                          [ Review → ]    │
└────────────────────────────────────────────────────────────┘
```

**Iteration count is displayed as a warning badge, not a neutral stat.** It is the single best proxy for overfitting risk a human can eyeball in one second.

Sort by recommendation strength × waiting time. Aging items grow more prominent — **nothing silently rots in the queue.**

### 3.2 Review view — the decision page

Five stacked sections, in this order. The order is deliberate: **the story first, the doubts second, the numbers third.**

> **Case-against panel (§3.2/②):** Built as a programmatic assembly — iteration count as a warning badge, closest-to-failing tests (ranked by margin), worst-performing regimes with unprofitable-fold counts, cost-breakeven multiplier, portfolio correlation above 0.5, and any knowledge entries with counter-evidence, rendered above the evidence section (never below).
>
> **Charts (§3.2/④):** Built via hand-coded inline SVG (`charts.py`). Replay computed on-demand per session (exact chain from `monitoring.replay` / Stage 11's validated path), cached per session, with caption noting this is NOT the concatenated walk-forward series the honest score was scored on — a material difference stated rather than hidden. Portfolio correlation computed fresh from stored return series, dashboard-only for v1 (not fed to A4).

**① The claim, in one sentence**
> *"JMA slope steepening combined with ATR expansion predicts multi-day breakouts in NSE mid-caps."*

Plain language, no jargon, no metrics. If the human cannot understand the hypothesis, that alone is grounds to reject.

**② The case against it** — placed *above* the case for it

A dedicated red-bordered panel:

- **Iteration count** and what it implies about multiple testing
- Which tests came closest to failing, **with margin**
- Which regimes it performed worst in, and how many folds were unprofitable
- Cost breakeven multiplier — *"edge disappears at 2.3× assumed costs"*
- **Correlation with existing live strategies** — ★ computed independently by the dashboard, not sourced from A4 (App-Flow §7.2); deliberately more than A4 itself saw
- Any contradicting knowledge entries — *"3 prior lessons say ATR > 3.0 overfits in this family; this uses 2.8"*

**This inversion is the single most important UI decision in the product.** Standard dashboards lead with green metrics and bury caveats. **We lead with the reasons to say no.**

**③ Evidence** — the robustness battery

A compact test matrix, not a wall of numbers:

```
Walk-forward       ████████░░  8/10 folds profitable     PASS
Honest score       0.62  (bar: 0.50)                     PASS
Deflated Sharpe    0.94  (156 family trials)             PASS
PBO                0.21  (threshold 0.35)                PASS
White's RC         p=0.03                                PASS
Monte Carlo P5     +4.2% CAGR                            PASS
Regime consistency ███████░░░  weak in high-vol          WARN
Cost sensitivity   breakeven 2.3×                        WARN
Parameter stability ████████░░                           PASS
```

Each row expands to method, threshold and reasoning. **Every threshold is visible** — a passing test with an invisible threshold is not evidence.

**④ Charts** — exactly four, no more

- Equity curve, with walk-forward test windows shaded distinctly from training
- Underwater (drawdown) curve
- Return distribution vs the Monte Carlo envelope
- Performance by regime (small multiples)

In-sample and out-of-sample must be **visually unmistakable** — different treatment, not just a legend entry.

**⑤ The research history** — collapsed by default

A timeline of all iterations: what changed, what the bar said each time. This is where a human catches *"it only started clearing after the third filter was added"* — the classic overfitting tell.

### 3.3 The decision control

```
        [ Reject ]   [ Request more research ]   [ Approve → Paper ]
```

- **No pre-selected default.**
- **Approve requires a typed note.** Friction on purpose — it forces articulation, and it becomes the data for scoring A4's calibration later. Recorded to `promotions.human_notes`.
- **Reject also requires a reason**, chosen from the structured failure categories plus free text. Rejection reasons flow to A5 and become knowledge.

---

## 4. Screen: Health

> ✅ **Built.** A flat list ranked by concern (computed as deviation from validated behavior), with colour semantics per §9.2 — green for "behaving as validated", not profit. Deployment detail includes live vs expected paired metrics with z-scores, regime context, execution quality, and promotion progress. Kill switch always reachable, confirmed with typed strategy name.

### 4.1 Deployment list

**Flat and ranked by concern — deliberately ignoring the Pipeline screen's grouping (§5.2).** A health concern can occur in paper or live, under any strategy, in any market; burying it in a tree is how you miss the one thing you needed to see.

```
🔴  Momentum Breakout v3     LIVE 4%    -12.4%    Behaviour diverged — sharpe z=-2.8
🟠  JMA Trend NSE            LIVE 2%     +3.1%    Slippage 2.1× expected
🟡  Mean Reversion Crypto    PAPER       -4.2%    Below expected win rate
🟢  ATR Channel Commodities  LIVE 5%     +8.7%    Healthy
🟢  Vol Target Forex         PAPER      +11.2%    Healthy · 187/250 trades
```

The status colour communicates **"is it behaving like what we validated?"** — never simply "is it up or down."

### 4.2 Deployment detail

The header states the verdict **in words, before any number**:

> **🟢 Behaving as validated.** Currently in a sideways regime where this strategy historically underperforms. The −6% drawdown is within the expected range for these conditions.

Then:

- **Live vs Expected** — paired bars for Sharpe, win rate, average trade, max DD, with the validated confidence band shaded. Deviation shown as a **z-score**, because "1.2 vs 1.6" means nothing without knowing the expected spread.
- **Regime context panel** — current regime, and whether the strategy historically struggles there. **This one panel prevents the most common bad decision:** killing a healthy strategy for an expected drawdown.
- **Execution quality** — expected vs actual slippage, missed fills, liquidity.
- **Promotion progress** (paper only) — trade count against requirement, regime coverage checklist.
- **Lifecycle timeline** — every event, who triggered it, why.

### 4.3 Kill switch

Always reachable, always confirmed with a typed strategy name. Never behind a menu. **Its presence is reassuring; its ease of use must not be.**

---

## 5. Screen: Pipeline

> ✅ **Built as designed**, deployment (`strategy × market × mode`) as the atomic unit, the lifecycle track, and the strategy/market grouping toggle all shipped exactly per §5.1–§5.4. **One bug caught before merge:** the "no strategies yet" empty state was also hiding the §5.3 data-quality queue — fixed to render the queue independently, since it must gate throughput regardless of whether any strategy exists yet. **A second bug, same fix location:** the HTTP layer never parsed a request's query string at all, so `?group=market` (§5.2's toggle) was silently dropped — fixed once in the server's dispatch, shared by every screen.

### 5.1 Lifecycle track

The primary layout is the **stage a deployment sits at**, not paper-vs-live:

```
Research → Awaiting review → Paper → Awaiting review → Live 1–5% → Live scaled → Retired
```

Strategies in active research show their current iteration and how close they are to the bar. Rejected strategies show structured reasons. **Quarantined** strategies need human debugging — the only place the human does technical work in the whole product.

### 5.2 Grouping toggle — strategy vs market

Neither is "correct"; they answer different questions, so this is a **toggle, not a committed hierarchy.**

**By strategy (default)** — how research and knowledge accrue:

```
JMA + ATR Trend   (14 family trials across 3 markets — see §5.3)
  ├── NSE Equity     LIVE 4%    🟢
  ├── Commodities    PAPER      🟡
  └── Crypto         PAPER      🔴
```

**By market** — the risk view. Everything in one market moves together in a crash, so aggregate exposure per market is what gets checked when things get ugly:

```
NSE Equity
  ├── JMA + ATR Trend       LIVE 4%
  ├── Mean Reversion        PAPER
  └── Vol Breakout          LIVE 2%
```

### 5.3 Data-quality queue ★

**The one recurring human task besides the two gates.** Unresolved `data_validation_flags` block their snapshot, and the scheduler will not dispatch experiments against a blocked snapshot (TRD §14.4) — so this queue directly gates throughput.

```
⚠ RELIANCE  2019-09-20   −49.8%  single bar, no matching corporate action
   [ genuine move ]  [ add missing action ]  [ data error ]

⚠ NIFTY-50 snapshot v3   universe_too_narrow — 51 distinct instruments
   across 25 years. Expected ~100–150 (TRD §14.3c)
```

Each flag is **either a real market event or a data error, and only a human can say which.** Resolution is one click plus, where a corporate action was missing, the action's terms.

Shown here rather than under System because it is a *research-blocking* decision, not a debugging view — but it is deliberately the only routine work in the product, and its queue length is worth watching: a growing backlog means the lab is throttled on data, not on ideas.

### 5.4 Family trial count on group headers ★

When grouped by strategy, the header shows the **family trial count**, not just the name.

The same idea tried across 3 markets is **not "one strategy, three markets" — it is three trials of the same idea**, and the deflated Sharpe reads it exactly that way (TRD §10.2). The dashboard must reinforce that reading, not quietly undermine it. Showing *"JMA+ATR (14 trials across 3 markets)"* keeps the honest framing visible at the exact moment a human is deciding whether the one good result is real or just the survivor.

---

## 6. Screen: Laboratory

> ✅ **Built, with stated known limits rather than silent gaps.** The funnel counts (Hypotheses → Implemented → Passed P0 → Cleared the bar) are proxies over the closest existing columns — there is no dedicated funnel-tracking table, and the page says so. **Reproducibility rate reads "not measured"**: nothing in the schema records a re-run against its original result for comparison. **Agent calibration** only scores A4's numeric `confidence` against the human's eventual decision — A1's `expected_behavior` and A3's `expected_effect` are free text with no comparable outcome to calibrate against, so those stay uncalibrated. Null-world FDR and repeat-failure rate render as designed, reading the metrics Stage 0/Stage 8 already operationalized.

The screen the Research Director cares about long-term. It answers: **is this thing getting smarter, or just busier?**

```
┌─ Funnel (last 30 days) ─────────────────────────────┐
│  Hypotheses      2,847  ████████████████████████    │
│  Implemented     2,610  ██████████████████████      │
│  Passed P0       1,982  █████████████████           │
│  Cleared the bar    11  ▏                           │
│  → Human review      3  ▏                           │
│  → Paper             1  ▏                           │
│                                                     │
│  Yield: 0.035%          Cost/discovery: $412        │
└─────────────────────────────────────────────────────┘
```

Panels, in order of importance:

- **Null-world false discovery rate** ★ — the headline integrity number (PRD §4.2). How many "discoveries" the pipeline reports on data with no alpha by construction. Shown with the date of the last calibration run and `max_score_observed` in noise — the bar any real result must clear. **If this rises, nothing else on this screen means anything.**
- **Repeat-failure rate** — how often did we test something memory should have killed? **Target → 0.** If it rises, A5 is not working and the whole premise is broken.
- **Reproducibility rate** — target 100%. Any deviation is an integrity emergency, displayed as such.
- **Knowledge growth** — new lessons and graph edges per week, split novel vs reinforcing.
- **Survival curve** — of strategies that reached live, how many remain healthy at 3/6/12 months. The only chart reflecting real-world truth.
- **Agent calibration** — A1 predicted `expected_behavior`; A3 predicted `expected_effect`; A4 gave a confidence. How often were they right? An overconfident agent is fixable, but only if measured.

---

## 7. Screen: Knowledge

> ✅ **Built as designed.** Lessons & rules, external claims, the knowledge graph, and the research-questions/curiosity queue all render from the tables Stage 8 (A5) and Stage 10 (the Librarian) now write for real — see Backend-Schema §9–§10 for which stage drives which table.

Browsable memory. **The two trust tiers are never mixed in one list** (PRD §8.3).

**Lessons & rules** (internal, tested) — searchable `knowledge_entries`, filterable by scope, market, confidence. Each shows evidence count **and counter-evidence count side by side**. A lesson with contradicting evidence must *look* visibly less certain.

**External claims** (untested) — `external_knowledge`, visually distinguished as candidates rather than facts, each traceable to the exact source passage via `document_chunks`.

**Knowledge graph** — interactive node-link view:
```
        ┌─ works in ──▶ Trending
Momentum┤
        └─ fails in ──▶ High Volatility   (14 experiments, confidence 0.87)
```
Edge thickness = evidence count. Clicking an edge lists the supporting experiments. **Every edge is traceable to experiments** — an unbacked edge is a bug, and the UI should make that visible rather than hide it.

**Research questions** — the curiosity queue. Open questions, what triggered them, and whether they ever produced a hypothesis. Directly answers *"is the curiosity loop actually closing?"*

---

## 8. Screen: System & Observability ★

> **Design only — Stage 4a not started.** Everything below remains unbuilt; it is the one screen in this brief the dashboard (Stage 12) does not cover. Stages 4–12 shipped without it — a terminal (`aqrl` CLI) and direct database inspection stood in.

**Not the decision layer.** This exists to debug the machine, not to approve capital — and it ships at **Stage 4a**, long before §3–§7.

**System basics:** job queue depth, running jobs, failure rates by class, budget consumption vs caps, scheduler heartbeat. Deliberately utilitarian. **Budget exhaustion is displayed as a normal state, not an error.**

### 8.1 Activity feed — which agent is doing what, right now

Sourced directly from the `jobs` table:

```
🟢 A2  Quant Engineer    experiment #4821   implementing...        12s
🟢 A3  Research Reviewer experiment #4819   reviewing...            3s
⚪ A1  Research Scientist   —                idle
✅ A4  Promotion         experiment #4802   done   (47s, $0.08)
❌ A2  Quant Engineer    experiment #4818   FAILED — compile error
```

Agent, target, status, duration, cost. **Every row deep-links into the data explorer**, so *"why is this stuck"* is one click from *"what actually happened."*

### 8.2 Data explorer — opening the database without a terminal

A read-only browser over the SQLite tables: table list, row viewer with filter and sort, and rows that link to each other (job → experiment → evaluation) instead of the human writing joins by hand.

Because this is local, single-user and strictly read-only, it also includes a **raw SQL box** — there is no audience beyond one to protect against, so a query builder would be wasted effort. **No write path exists in this view, ever.**

### 8.3 The one deliberate exception to "no polling"

§12 sets manual-refresh-only as the default, specifically so the product stays calm rather than casino-like. **The activity feed is the only exception** — a live "what's running" view that is twenty seconds stale defeats its own purpose. Everything else, including the data explorer, stays manual-refresh.

---

## 9. Visual Language

### 9.1 Principles

| Principle | Application |
|---|---|
| **Evidence over aesthetics** | Every number carries its threshold or confidence interval |
| **Doubt is visible** | Warnings are never collapsed by default; the case-against precedes the case-for |
| **Comparison over absolutes** | Live metrics always paired with their validated expectation |
| **Traceability is one click** | Any claim expands to the evidence behind it |
| **Calm by default** | No animation, no auto-refresh flicker. This is a laboratory, not a casino |

### 9.2 Colour semantics — fixed, never decorative

| Colour | Meaning | Used for |
|---|---|---|
| Green | Behaving as validated | Health only — **never** "profitable" |
| Yellow | Deteriorating, still plausible | Health, warnings |
| Orange | Multiple warning signals | Health, pending action |
| Red | Diverged from the validated model | Health, hard failures, kill switch |
| Neutral grey | Informational | Everything else |

**Profit and loss use a separate, non-red/green scale** to avoid collision with health semantics. This is deliberate: **a losing strategy is not a red strategy**, and conflating the two is exactly the confusion the whole health framework exists to prevent.

### 9.3 Charts

- In-sample vs out-of-sample must be distinguishable **without reading a legend**
- Monte Carlo envelopes as shaded bands, the actual path overlaid
- **Every threshold drawn as an explicit reference line**
- No dual axes, no 3D, no pie charts
- Every chart works in light and dark themes
- Wide tables and charts scroll inside their own container; the page never scrolls horizontally

### 9.4 Typography & density

Information-dense but not cramped. Metrics in tabular figures so columns align. **The review screen is the one place where generous whitespace matters** — the human is making a capital-allocation decision and should not feel rushed.

---

## 10. Interaction Rules

1. **No destructive action without typed confirmation.** Kill switch, retire, reject-with-prejudice.
2. **No approval without a written note.** Friction is the point.
3. **Nothing auto-promotes.** Ever. There is no timeout that advances a strategy.
4. **Every screen is read-only except the decision controls.** The dashboard cannot edit strategies, code, or knowledge.
5. **Keyboard-first for the queue.** Reviewing many candidates should not require a mouse.
6. **Deep links everywhere.** Any experiment, lesson or deployment is addressable by URL.

---

## 11. Notifications

Deliberately sparse. The system should be quiet enough that a notification means something.

| Event | Channel |
|---|---|
| New promotion candidate | Dashboard badge + daily digest |
| Health → 🟠 orange | Dashboard + immediate push |
| Health → 🔴 red | Dashboard + immediate push (auto-halt already executed) |
| Kill switch fired | Immediate push |
| Scheduler down > 30 min | Immediate push |
| **Null-world FDR rises above threshold** | Immediate push — an integrity emergency |
| Budget exhausted | Daily digest only |
| Weekly lab report | Email |

**A novel discovery is not urgent. A degrading live strategy is.**

---

## 12. Technology

| Concern | Choice |
|---|---|
| v1 | Local web app, **read-only** against SQLite + Parquet/DuckDB |
| Rendering | Server-rendered pages; minimal client JS |
| Charts | Static / lightweight — no heavy dashboarding framework |
| Auth | None in v1 (localhost); added when hosted |
| Refresh | Manual + on-navigation everywhere **except the activity feed** (§8.3) |

---

## 13. Open UX Questions

- [ ] Should the review screen hide A4's recommendation until the human has read the evidence, to avoid anchoring?
- [ ] Mobile: read-only health monitoring, or approvals too? (Leaning read-only — capital decisions deserve a full screen.)
- [ ] How much of a 47-iteration history is shown by default before it becomes noise?
- [ ] Should rejected strategies remain browsable indefinitely, or be archived out of the main views after N days?
- [ ] Does the data explorer (§8.2) need row-level access control before it is ever exposed beyond localhost, given the raw SQL box?
- [ ] Retention on the activity feed — how far back does job history stay live-browsable?

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial brief — "make it easy to reject" premise, case-against-first review layout, health-not-profit colour semantics, laboratory self-measurement screen, sparse notification policy. |
| 2026-07-28 | Restructured navigation: paper/live replaced by a lifecycle-track Pipeline screen with deployment as the atomic unit and a strategy/market grouping toggle; family trial count on group headers. Added Observability, shipped early and kept structurally separate from the decision layer. |
| 2026-07-28 | **Full rewrite.** Sequential numbering §0–§13; the two build waves stated up front; Knowledge screen separates the two trust tiers. |
| 2026-07-28 | Added the **data-quality queue** (§5.5) — unresolved validation flags block their snapshot, so resolving them is a real human task the dashboard must surface. Cross-references updated to the renumbered TRD. |
| 2026-08-05 | **This brief's dashboard is built (Stage 12)**, `aqrl/dashboard/`, stdlib-only, all five screens (Decisions → Health → Pipeline → Laboratory → Knowledge) in the specified order. ✅ Built annotations added per screen, noting the Decisions screen's new third button (Defer, via `gates.defer` + migration `0010_promotion_defer.sql`), two real bugs caught and fixed before merge on the Pipeline screen, and known limits stated on the Laboratory screen (funnel counts are proxies, reproducibility rate unmeasured). §8 System & Observability remains design-only — Stage 4a not started. See `Implementation_Plan.md` §15 for full detail. |
