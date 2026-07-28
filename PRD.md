# PRD — AQRL (Autonomous Quantitative Research Laboratory)

> **Status:** Design complete for v1. No implementation started.
> **Last updated:** 2026-07-28
> **Companion docs:** `TRD.md` (how) · `Backend-Schema.md` (data) · `App-Flow.md` (sequences) · `Implementation_Plan.md` (build order) · `UI-UX-Brief.md` (interface)
> **Design reference:** [karpathy/autoresearch](https://github.com/karpathy/autoresearch) — see §13

---

## 1. Vision

We are not building an AI hedge fund. We are building an **autonomous scientific laboratory that discovers, tests, and refines quantitative trading knowledge using reproducible evidence.**

> An AI hedge fund says: *"We use AI to trade."*
> AQRL says: *"We build machines that discover and validate financial knowledge."*

The distinction changes what we optimise. A good strategy has a 2–5 year edge. A good research engine keeps discovering strategies for decades. **Strategies are outputs of the system; the system is the asset.**

### 1.1 The governing analogy — drug discovery

Pharma does not expect every molecule to become a medicine. It runs a funnel where cheap screens come first and expensive trials come last, and it documents *why* candidates failed as rigorously as why they succeeded.

| Pharma | AQRL |
|---|---|
| Millions of molecules | Millions of hypotheses |
| Initial screening | **P0** — code correctness, no look-ahead |
| Lab testing | **P1** — fast backtest, small slice |
| Animal trials | **P2** — full history, realistic costs |
| Human trials | **P3** — walk-forward, Monte Carlo, PBO, regime, stress |
| FDA approval | Human dashboard review |
| Post-market surveillance | **P4** — paper trading, then monitored live |

The expensive tests happen **last**. Most ideas die early and cheaply.

### 1.2 What "success" looks like at maturity

Not a strategy. A continuously growing body of evidence about what works, where, and under what conditions — from which strategies are drawn as needed, and to which every retired strategy contributes its post-mortem.

---

## 2. Problem Statement

Quant research is bottlenecked not by idea generation but by **reliably distinguishing genuine alpha from overfitting**, and by institutional knowledge living in scattered notebooks and human memory.

| # | Problem | Consequence |
|---|---|---|
| **P1** | Idea throughput | A human tests a handful of hypotheses per week; most are never recorded |
| **P2** | Validation rigour | Ad-hoc backtests silently leak future information, ignore costs, overfit to one regime |
| **P3** | Amnesia | Failed experiments aren't stored structurally, so dead ends get re-explored indefinitely |
| **P4** | No feedback loop | Live performance rarely flows back into research |
| **P5** | Unclear stop rules | Nobody knows when a live strategy is dead versus in a normal losing streak |

---

## 3. Non-Goals

Explicitly **out of scope**, permanently or for now:

- ❌ **Automatic promotion to live trading.** A human approves before paper trading and again before live capital. Never negotiable.
- ❌ **Multi-strategy portfolio construction and correlation-aware allocation.** A4 (§6.3) judges each strategy on its own merits only. Portfolio fit is shown to the human as dashboard context, computed independently, and is never fed into any agent's decision. A dedicated portfolio-construction capability is future scope — see §10.4 for why this deferral is safe.
- ❌ Beating Renaissance/Two Sigma at their own game. We compete on research *workflow*, not data budget or headcount.
- ❌ HFT / latency-sensitive execution. The lab may study sub-minute data but will not compete on speed.
- ❌ Options and exotic derivatives in v1.
- ❌ Claude performing mechanical work (downloading PDFs, renaming files, moving data). Claude reasons; Python does everything else.
- ❌ Free-form formula invention by the LLM. Strategies compose from a vetted operator library.
- ❌ Building for 100,000 experiments/day on day one. The architecture must *allow* that scale; v1 runs on a laptop.

---

## 4. Success Criteria

### 4.1 The v1 KPI — a single question

> **Can AQRL independently discover one statistically valid strategy that a human did not explicitly design?**

Once proves the concept. Repeatedly across markets means something rare. Everything after that is horizontal scaling, not redesign.

### 4.2 The headline integrity metric — null-world false discovery rate ★

**The single most important number the laboratory produces about itself, and it gates everything else.**

Run the entire loop on data with **no alpha by construction** — permuted returns, block-bootstrapped noise, or synthetic paths with matched volatility and fat tails. Count how many "statistically valid discoveries" it reports.

| Result | Meaning |
|---|---|
| **0 discoveries** | The pipeline is honest. Real results can be trusted. |
| **12 discoveries** | The pipeline manufactures ~12 findings from nothing, and *every* real discovery it has ever produced is suspect. |

This is the only way to know whether the machine works. It is a **permanent regression test** — re-run after every change to `evaluate.py`, every new profile, every change to the scoring rule.

**No real-data result may be trusted before null-world FDR has been measured and driven low.** This is Milestone 0 (Implementation_Plan §17).

### 4.3 What we deliberately do *not* measure

Not "number of strategies generated," not "number of backtests run." Generating 10,000 backtests is trivial and actively harmful — it inflates the multiple-testing burden without discipline.

### 4.4 What we do measure

| Metric | Why it matters |
|---|---|
| **Null-world false discovery rate** | §4.2 — if this rises, nothing else on this list means anything |
| **Experiments reproducible from stored artifacts** | Scientific integrity — target 100% |
| **Repeat-failure rate** (ideas re-tested that memory should have killed) | Is memory actually working? Target → 0 |
| Hypotheses generated · experiments completed | Input volume and throughput |
| Strategies passing all P3 gates | Real funnel yield |
| Strategies surviving paper trading | Forward-evidence yield |
| Strategies still healthy at 3 / 6 / 12 months live | The only metric that pays |
| Novel lessons added to the knowledge base | Is the lab learning? |
| Cost per credible discovery | Efficiency of the funnel |

### 4.5 Research portfolio allocation

Borrowed from pharma R&D. A1's hypothesis budget is split:

- **70%** — incremental improvement of proven strategy families
- **20%** — adapting ideas across markets and asset classes
- **10%** — high-risk unconventional ideas

---

## 5. Users & Roles

| Role | Who | Interaction |
|---|---|---|
| **Research Director** | You (human) | Sets goals and budgets, reviews the dashboard, approves both gates, kills strategies |
| **The Laboratory** | 5 in-loop agents + the Librarian | Runs autonomously between human gates |
| **Reviewer** (future) | Additional humans | Read-only dashboard, can comment on candidates |

The human is a **gatekeeper and goal-setter, not an operator.** If the human is doing routine work, the design has failed.

---

## 6. The Agents

Five agents form the research loop. A sixth — the Librarian — runs outside it.

| # | Role | The single question it answers | Output |
|---|---|---|---|
| **A1** | Research Scientist | "What is worth investigating next?" | Strategy spec |
| **A2** | Quant Engineer | "How do I implement this correctly?" | Executable strategy code |
| **A3** | Research Reviewer | "This failed the bar — what should we change?" | Research plan, or a verdict to stop |
| **A4** | Promotion Committee | "Should this advance to the next stage?" | Promotion recommendation |
| **A5** | Knowledge Manager | "What did the laboratory learn?" | Knowledge entries + graph updates |
| **—** | **Librarian** | "What is this external document saying?" | Structured external-knowledge records |

### 6.1 Why A3 produces a plan, never code

A3 says *"replace the fixed stop with an ATR trailing stop, because exits are cutting winners short in trending regimes."* It does **not** write the function — A2 decides how. This keeps the scientist separate from the engineer and makes A3's reasoning reviewable in plain language.

### 6.2 Why A4 and A5 are separate

They answer different questions and would overload one role as the archive grows. A4 asks *"should this advance?"*; A5 asks *"what did we learn?"* A4 works within one strategy's history; **A5 works across experiments** — its value emerges from noticing that experiments #25, #193 and #6201 all failed for the same reason and promoting that into a family-scoped rule.

### 6.3 What A4 sees, and deliberately does not

A4 receives the **entire research history**, not just the winning result. A strategy that cleared the bar on iteration 31 of a 47-try grind is a fundamentally different object from one that cleared it on try 2 — that iteration count *is* the overfitting signal, and A4 must be able to tell them apart.

A4 assesses **capacity and liquidity** — can this strategy alone trade at real size. It does **not** assess portfolio correlation (§3): that is portfolio-construction work, out of scope for v1, and judging a strategy on its own merits keeps A4's job coherent.

**A4 can reject or defer on its own authority. It can never approve on its own** — approval only ever produces a recommendation that a human must confirm.

### 6.4 The Librarian sits outside the loop ★

A1–A5 iterate on one strategy at a time inside Research → Code → Evaluate → Review → Promote. The Librarian runs on its own schedule, reads external documents, and only ever *feeds* A1. It is never invoked by A3 or A4 and never blocks an experiment.

| | A5 — Knowledge Manager | Librarian |
|---|---|---|
| Reads | **Our own** experiment results | **Other people's** papers, books, code, blogs |
| Question | "What did we learn from our test?" | "What is this document saying?" |
| Output trust | High — our own tested evidence | Low — a claim, not yet tested (§8.3) |

Kept separate for the same reason A4 and A5 are: different inputs, different failure modes.

Full mechanics in TRD §12.2; output schema in Backend-Schema §10.

---

## 7. The Closed Loop

```
        External Knowledge              Internal Knowledge
        (papers, code, market)          (experiments, lessons, live results)
                 │                                │
                 └──────────────┬─────────────────┘
                                ▼
                     A1 — generate hypothesis
                                ▼
                     A2 — implement strategy
                                ▼
                        evaluate.py
                                ▼
                    ┌── cleared the bar? ──┐
                   NO                     YES
                    ▼                       ▼
              A3 — review              A4 — promotion
              iterate ──┐                   ▼
              or stop   │              ╔═══════════╗
                    └───┘              ║ HUMAN GATE ║
                    ▼                  ╚═══════════╝
              A5 — record                     ▼
              the lesson                Paper → Gate → Live
                    │                          │
                    └──────────┬───────────────┘
                               ▼
                     better hypotheses ───↺
```

**Every experiment reaches A5**, whether it succeeded or failed. Nothing is discarded without a lesson.

### 7.1 The loop only improves if it receives new information ★

The most important caveat in the design. A closed loop on a fixed dataset with a fixed operator library will hit diminishing returns and begin rediscovering variants of the same ideas — while *appearing* busy. Required inputs:

- New market data (daily)
- New academic literature (continuous ingestion via the Librarian)
- New operators added to the library (human-gated)
- Live and paper trading results fed back
- Research questions generated by the lab's own failures

### 7.2 The Curiosity Engine

The lab must not passively consume external knowledge. A failure **generates a targeted search**:

```
Observation:  "Momentum strategies fail in high-volatility regimes"
      ↓
Research question: "Find literature on volatility-adaptive momentum"
      ↓
Queued for the collectors → Librarian extracts → A1 consumes
      ↓
Better hypothesis next cycle
```

This is the difference between a retrieval system and a researcher. Each question tracks whether it *ever produced a usable hypothesis*, so the loop's own value is auditable.

---

## 8. Knowledge Architecture

Two knowledge bases with fundamentally different trust levels. **They must never be conflated.**

### 8.1 Internal Knowledge — the lab's own memory

**Ground truth**, because we generated it under controlled conditions. Append-only; never overwritten.

It has **two layers**:

| Layer | Contents | Written by | Cost |
|---|---|---|---|
| **Raw record** | Every experiment, its full evaluation results, code commit, provenance | Automatically, every experiment, no LLM | Free — a database write |
| **Synthesized lesson** | Lab notebooks, knowledge entries, graph edges | A5, once per strategy at the end of its story | An LLM call |

The raw layer captures *everything* as it happens. A5 then reads the complete set — all iterations together — and writes one well-formed lesson, rather than a half-formed summary after each attempt. Nothing is lost; the synthesis is just better-informed and cheaper for being done once.

**This is the moat.** Not Claude Code — anyone can use Claude Code. The moat is a large, well-curated experiment database plus a validation pipeline that reliably kills weak ideas.

### 8.2 External Knowledge — the world's memory

Four layers:

| Layer | Contents |
|---|---|
| Research | Papers, books, indicators, validation techniques |
| Market | Current regime stats, volatility, correlations, liquidity |
| Software | New libraries, faster algorithms, better optimisers |
| Infrastructure | Better testing, validation, risk methodology |

**Store knowledge, not documents.** The Librarian converts each document once. A 40-page paper does **not** become one blob of summary — it typically contains several distinct usable ideas, and **each becomes its own record**. The document is read exactly once, ever; every later access is to the structured records.

**Claude does not browse the web.** Python collectors gather and pre-process; the Librarian consumes prepared documents.

### 8.3 A claim is not a fact ★

Everything the Librarian writes is tagged `evidence_tier = external_claim`. Its confidence field measures **the Librarian's confidence that it read the source correctly** — never a claim that the underlying idea is true.

Only Internal Knowledge carries tested-evidence weight. **A strategy is never promoted because "a paper said so"** — only because our own experiments confirmed it. An external claim is a candidate worth testing; it earns promotion to real knowledge only by surviving our own pipeline.

---

## 9. Strategy Lifecycle & Promotion Rules

### 9.1 Stages

```
Idea → Spec → Code → Evaluate → [bar cleared] → A4
  → ╔ HUMAN GATE ╗ → Paper Trading → ╔ HUMAN GATE ╗ → Live 1–5% → Scale Up → Full
                                                              │
                                              Monitor → Retire / Return to research
```

### 9.2 The iteration stop rule ★

**Rule #1, overriding everything else: clearing the acceptance bar is an immediate, unconditional stop.**

The moment any iteration clears the bar, that strategy stops iterating — permanently, right then — and goes straight to A4. This is not a judgment call A3 gets to make; the worker enforces it in Python before A3 is even invoked (App-Flow §6.1).

*Why it must be a rule and not a judgment:* this is what satisficing (§10.2) means in practice. The bar already includes a minimum score, so clearing it already means "good enough by a standard set in advance, on purpose, before searching." Leaving "should I push for more?" as an agent decision would let an LLM quietly override the principle one plausible justification at a time. Every further iteration would spend more of the family's finite trial budget and more irreplaceable out-of-sample data chasing a number nobody asked for.

**Below the bar**, A3 operates and stops for any of:

| Condition | Routes to |
|---|---|
| **5 consecutive bar failures**, never once cleared (plateau — default, configurable) | **A5** — never A4; there is nothing bar-passing to review |
| Iteration / token / compute budget exhausted before ever clearing | **A5**, same reason |
| A3 judges further modification futile | **A5** (reject) |
| Hard iteration cap (~20–25) — backstop only | **A5** (forced plateau) |

**Iteration count is recorded and passed to A4 as an overfitting signal** regardless of outcome.

### 9.3 Paper trading promotion — trades, not calendar

Time is only a proxy. What we need is **enough independent observations**. A strategy trading 5×/year needs far longer than one trading 5×/day.

| Strategy type | Minimum trades | Better | Excellent |
|---|---:|---:|---:|
| High frequency | 5,000 | 20,000 | 100,000+ |
| Intraday | 500 | 1,000 | 2,000+ |
| Swing | 100 | 250 | 500+ |
| Position | 50 | 100 | 200+ |

Trade count alone is insufficient. **All** of the following are required:

- ✅ Minimum trade count reached
- ✅ Live Sharpe / profit factor / max DD within acceptable deviation of validated expectations
- ✅ Coverage of multiple regimes (trending, sideways, high-vol, low-vol)
- ✅ No abnormal slippage, missed fills, or execution errors
- ✅ Statistical health checks show behaviour consistent with the validated model

The question is never *"has it paper traded for 6 months?"* but **"have we collected enough high-quality evidence to justify risking real capital?"**

### 9.4 Live monitoring — the health question ★

Never decide on drawdown alone. The question is not *"has this lost money?"* but:

> **"Is this strategy still behaving like the strategy we originally validated?"**

A good strategy can lose money for months and remain healthy. A bad strategy can make money for months on luck while its underlying characteristics have already broken.

Monitored dimensions: performance, statistical behaviour (win rate, average trade, loss distribution vs expected), **market regime context**, and execution quality.

| Level | Condition | Action |
|---|---|---|
| 🟢 Green | Within historical expectations | Continue |
| 🟡 Yellow | Deteriorating but statistically plausible | Reduce size, investigate |
| 🟠 Orange | Multiple warning signals | Pause new capital, revalidate on recent data |
| 🔴 Red | Live behaviour no longer matches the validated model | Stop, return to the research pipeline |

**Regime context prevents the most common bad decision.** A drawdown occurring in a regime where the strategy historically struggled is *expected behaviour*, not evidence of death.

A 20% drawdown may be perfectly acceptable if validation showed a 15–25% range. A 100% drawdown means risk limits and kill switches failed long before — **hard limits must make that state structurally unreachable** (TRD §18).

---

## 10. The Search Objective

### 10.1 The lab never stops — the objective evolves, the search does not pause ★

**AQRL runs continuously, 24/7, and does not stop on success.** Clearing the bar stops *that strategy's* iteration (§9.2); it does not stop the campaign or the laboratory. The lab keeps discovering alphas and feeding them into paper trading as a **continuous pipeline**, with many strategies at different lifecycle stages simultaneously.

What changes over time is the *objective A1 optimises for*, not whether the search is running:

| Phase | Objective | Active when |
|---|---|---|
| **A** | *"Find strategies that are tradeable, risk-controlled and robust."* | Always. Never switched off |
| **B** | *"Find strategies that make money when the ones already deployed don't."* | Layers on top of A, once strategies are live and their return series exist to measure against |

You cannot build a portfolio from zero strategies, so Phase A comes first in *time* — but it is never *replaced*. Phase B is an additional lens applied to candidates that already cleared Phase A's bar, not a different bar.

**The v1 KPI (§4.1) is the first strategy discovered this way**; the lab continuing past it is the point, not a deviation.

**Idle is not the same as throttled.** No agent should sit idle because nothing was queued — that is a scheduling bug. Agents *may* idle because a budget cap, the autonomy ratchet, or a vault budget was reached; those are the safety mechanisms working. TRD §4.5 makes the distinction operational.

### 10.2 Satisficing, not maximising ★

**"Find the best possible strategy" is the wrong instruction**, even in Phase A.

If the objective is *best*, the loop never stops — it grinds for a higher number, and every extra attempt burns another trial against the same data. That is how a strategy ends up excellent on history and mediocre in reality.

Instead: **write the acceptance bar down before the search begins**, and take the **first** strategy that clears it.

The bar is decided in advance and lives in **two places with different jobs** (TRD §7.5):

- **`program.md`** — states the bar so the agent knows the target
- **`evaluate.py`** — *enforces* it, so the agent cannot grade its own homework

| The bar (pass/fail, pre-registered) | Value |
|---|---|
| Minimum honest score (TRD §7) | **0.50** |
| Maximum out-of-sample drawdown | **15%** — **20% for crypto** |
| Minimum trade count | **100** |
| Minimum breadth across instruments | *definition still open* |
| Maximum complexity | *definition still open* |
| Must survive costs at 2× the assumed level | required |

Failing any item returns `discard` with **no score computed**. The bar gates; the score ranks.

**Two independent reasons to stop early:**

1. **Selection bias.** The strategy found on attempt 400 is mostly better *at fitting history*.
2. **Out-of-sample data is a consumable resource.** Every iteration against the walk-forward window makes it slightly less out-of-sample, until it has simply been fitted more slowly. This resource cannot be refilled.

### 10.3 Ranked acceptance criteria

1. **Primary** — the honest score (TRD §7)
2. **Secondary** — a resource constraint: capacity or turnover
3. **Tertiary** — **simplicity.** Where two strategies score alike, the simpler wins

Simplicity is a *scored criterion*, not reviewer judgment. A strategy with 7 filters must beat one with 2 filters by a real margin, not a hair. Complexity is one of the few reliable predictors of overfitting, so it must cost something.

### 10.4 Why diversification is an anti-overfitting device ★

Phase B is frequently misread as "accept worse strategies." It is not. There are two distinct questions:

**Question 1 — "Is this tradeable at all?"** A pass/fail floor, non-negotiable: risk controlled, survives out-of-sample, survives realistic costs, actually executable, drawdown within limits, mechanism understood. Diversity never rescues a failing strategy.

**Question 2 — "Given what I already run, is this worth adding?"** Asked *only* of strategies that already cleared Question 1.

**The arithmetic:** two genuinely uncorrelated strategies at Sharpe 1.0 each combine to ≈1.41; three to ≈1.73; four to 2.0. A strategy at **Sharpe 0.7 that profits when the existing one bleeds is worth more** than one at Sharpe 1.3 that profits at the same time. A strategy's value is measured against the book, not in isolation.

The same holds for drawdown. Per-strategy limits are unchanged, but the number that matters is **portfolio** drawdown — two strategies whose bad periods occur at different times produce a combined drawdown smaller than either alone. Diversification is stronger drawdown control than tightening stops on a single strategy.

**The decisive argument is about overfitting.** Trend following fails in sideways markets; that is inherent to the logic, not a defect. The natural response — adding filters and regime detectors until it performs well in every historical period — does not fix the weakness. It *memorises where the weakness occurred in this particular history*. The honest alternative is to accept the specialisation and find a **different** strategy for that regime. "Works everywhere" is usually a fitted illusion.

### 10.5 Fake diversification

These are **not** diverse — the same bet in different clothes, and their correlation rises precisely during crises:

- The same strategy across 20 instruments
- The same logic at different lookback lengths
- Trend following on NIFTY and on Bank NIFTY
- Trend following on equities and on commodities

Genuine diversity is difference in **logic**: trend vs mean reversion, long vs short holding period, volatility breakout vs volatility selling, price-based vs volume-based vs cross-sectional.

The test is not whether it *feels* different — it is whether the return series actually move independently, measured including correlation during each strategy's worst months.

---

## 11. Foundational Principles

1. **Every decision answers "why did you do this?"** Every hypothesis, code change, promotion and retirement carries a traceable, data-backed explanation. This buys reproducibility, auditability, faster debugging, and the ability to improve the system rather than treat it as a black box.
2. **The lab is self-critical, not just self-creative.** For every agent proposing ideas, there is machinery whose only job is to falsify them.
3. **Knowledge is the central asset**, not any individual strategy.
4. **Failures are data.** A rejected experiment must answer: why did it fail, was it overfit, was it regime-specific, did costs kill it?
5. **Every experiment generates the next experiments.** A lab notebook entry ending without proposed follow-ups is incomplete — that is what makes the system self-propelling.
6. **Comparability is sacred.** One evaluation engine, versioned, profile-driven.
7. **Structural beats procedural.** Where a rule can be enforced by making violation impossible rather than by instructing an agent not to violate it, it must be.
8. **Markets adapt.** No lab produces a constant stream of durable alpha. The value is that it keeps searching as edges decay.

---

## 12. Scope — Markets & Data

### 12.1 Markets and instruments

| Market | Instruments traded | Notes |
|---|---|---|
| **Indian Equities** | Cash equity | **Universe: NIFTY-50 constituents only.** ⚠️ Survivorship unresolved — see §12.3 |
| **Indian Indices** | Futures, ETFs | No survivorship problem — index-level |
| **US Indices** | CFDs, ETFs | |
| **Forex** | CFDs | Spread-driven costs |
| **Commodities** | Futures, CFDs | Contract-roll handling required |
| **Crypto** | Spot, perpetuals | 24/7; funding rates on perps; 20% drawdown limit vs 15% elsewhere |

Cash indices are not directly tradeable — every index exposure is via a future, ETF or CFD, and the **cost model is keyed on `(market, asset_class)`**, not market alone (TRD §6.3).

### 12.2 Timeframes

**1 second to 1 month.** The architecture must span the full range; every timeframe-dependent value is profile-driven (TRD §6.4). Below ~1 minute, backtest fidelity degrades because fills depend on queue position and latency that bar data cannot represent — supported, but not trustworthy without live confirmation.

### 12.3 Capital

| Stage | Amount |
|---|---|
| Paper trading | Unconstrained — any notional |
| **Initial live** | **₹10 lakh** (~US$12,000) |

At ₹10 lakh, a full 50-stock basket is thin — roughly ₹20,000 per position. Concentrated baskets (10–20 names) or index instruments are the more realistic starting shape. Capacity is not a binding constraint at this size, but **transaction costs are**: at ~30 bps round trip (TRD §6.3), small positions are disproportionately eroded.

### 12.4 Known data gaps ⚠️

Market data is supplied manually as offline Parquet. Two quality problems affect Indian equities, and they compound — both inflate backtest results in the same direction.

| Gap | Status | Consequence |
|---|---|---|
| **No point-in-time NIFTY-50 membership** | ⚠️ **Fix chosen, blocked on data** | Survivorship bias. Resolved by collecting historical index membership **plus price history for all ~100–150 ever-members** — the companies that left are the invisible losses. **Blocks live capital on Indian equities until both exist** (TRD §14.3, §20.1) |
| **Source prices are unadjusted** | ✅ **Mitigated by design** | Splits and bonuses read as phantom ±50% moves. Handled by the Stage 1 corporate-action pipeline plus an unexplained-jump validator (TRD §15.2) |

Index-level research (NIFTY futures, ETFs) is structurally unaffected by both — it should run while the equity data is assembled, so the lab is never blocked.

Seed knowledge for the internal and external stores is hand-written by the Research Director, which resolves the cold-start problem for A1.

A prior prototype exists in git history at `d08e812`, mapped to the stages that reuse it in Implementation_Plan §19.

---

## 13. Design Reference — karpathy/autoresearch

**What it is:** three files — `prepare.py` (fixed; agent may read but never edit), `train.py` (**the only file the agent edits**), `program.md` (instructions, **edited by humans**). A fixed 5-minute wall-clock budget per experiment. The metric is `val_bpb`. Improved → keep the commit; worse or equal → `git reset`. ~100 experiments overnight. The agent cannot modify the evaluation harness or install dependencies. Ranked criteria with simplicity as tertiary. Once running, it does not stop to ask the human whether to continue.

### 13.1 What we adopt

- The **file structure and its permission boundary**
- **Evaluator isolation enforced structurally**, not by instruction
- **One fixed invariant** that makes all results comparable
- **Simplicity as a ranked scoring criterion**
- **`program.md` edited by humans, never the agent** — the honest version of "the lab learns"
- **No human interruption of the research loop** (gates exist only at paper and live)
- **Explicit `keep` / `discard` / `crash` status** on every experiment
- **One branch per campaign** in a single repo — scaled here to one branch per strategy

### 13.2 What does not transfer, and why it is the most load-bearing distinction ★

**His metric is nearly unhackable; ours is trivially hackable.**

`val_bpb` is a held-out likelihood. Running 100 experiments and keeping the best is epistemically sound — the held-out set was never trained on. Running 100 backtests and keeping the best Sharpe is a **selection-bias failure**: with enough attempts, noise produces a beautiful equity curve.

So the reference's encouragement toward high throughput and keep-if-improved makes our integrity machinery **more** necessary, not less:

- **The vault** (TRD §15.2) — data the loop physically cannot read
- **Null-world calibration** (§4.2) — measuring how often we invent discoveries
- **Satisficing** (§10.2) — stopping early rather than searching for the maximum

**Adopt the throughput. Do not adopt the keep/discard rule unmodified.** In his setting, improvement-on-metric is evidence. In ours, it is a hypothesis.

---

## 14. Open Questions

Owner marked where the decision is the human's to make.

**Numeric choices, pre-registered before searching:**
- [ ] The acceptance bar values — min score, max OOS drawdown, min trades, breadth, complexity cap. *Owner: human*
- [ ] Confidence level on the score's uncertainty haircut — `2×SE` (~97.5% one-sided) or `1.65×SE` (~95%, more candidates survive). *Owner: human*
- [ ] Which specific P3 statistical tests are gating (hard fail) vs advisory

**Design questions still open:**
- [ ] Does the agent tune parameters per fold, or write fixed-parameter strategies? Changes what walk-forward tests and how `evaluate.py` is built (TRD §8.5). *Owner: human*
- [ ] Whether A1 hypothesis generation is nightly-batch, purely event-driven, or both
- [ ] Capacity/AUM modelling — at what point does liquidity invalidate a backtest
- [ ] Human review SLA — how long may a candidate sit in the queue
- [ ] Does an `external_claim` ever earn a higher trust tier from repeated corroboration across many papers, or strictly only via our own tested experiments (§8.3)?
- [ ] Broker/data-feed choice for paper trading per market

**Deferred to a future portfolio-construction capability (§3):**
- [ ] How "genuinely different" is measured for Phase B admission — correlation ceiling, and over which window
- [ ] Correlation ceiling for admitting a new strategy to the live portfolio

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial document — vision, agent architecture, promotion rules, health monitoring, portfolio allocation, success criteria. |
| 2026-07-27 | Reconciled against karpathy/autoresearch. Added null-world FDR as the headline integrity metric, the two-phase search objective, satisficing over maximising, and the diversification-as-anti-overfitting argument. Added the Librarian and the two-trust-tier knowledge distinction. |
| 2026-07-28 | Clearing the bar became an immediate stop; portfolio-correlation checks removed from A4 and added to non-goals. |
| 2026-07-28 | **Design decisions locked in** — bar values (min score 0.50, max DD 15% / 20% crypto, min trades 100, z = 1.65), continuous 24/7 operation with Phase B layering onto Phase A rather than replacing it, the final market and instrument list, 1s–1month timeframes, ₹10 lakh initial live capital, and the two Indian-equity data gaps. |
| 2026-07-28 | **Full rewrite.** Integrated the locked-in decisions into the body rather than as appended edits; §12.4 restated as two compounding data gaps with survivorship now fix-chosen-blocked-on-data; cross-references updated to the renumbered TRD; changelog consolidated. No decisions changed. |
