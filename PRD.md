# PRD — AQRL (Autonomous Quantitative Research Laboratory)

> **Status:** Living document. Updated after every design session.
> **Last updated:** 2026-07-27
> **Phase:** Architecture / brainstorming. No implementation started.
> **Primary design reference:** [karpathy/autoresearch](https://github.com/karpathy/autoresearch) — see §14.

---

## 1. Vision

We are not building an AI hedge fund. We are building an **autonomous scientific laboratory that discovers, tests, and refines quantitative trading knowledge using reproducible evidence.**

> An AI hedge fund says: "We use AI to trade."
> AQRL says: "We build machines that discover and validate financial knowledge."

The distinction matters because it changes what we optimize. A good strategy has a 2–5 year edge. A good research engine keeps discovering strategies for decades. **Strategies are outputs of the system; the system is the asset.**

### 1.1 The governing analogy — drug discovery

Pharma does not expect every molecule to become a medicine. It runs a funnel where cheap screens come first and expensive trials come last, and it documents *why* candidates failed as rigorously as why they succeeded. AQRL adopts this wholesale:

| Pharma | AQRL |
|---|---|
| Millions of molecules | Millions of hypotheses |
| Initial screening | Phase 0 — code correctness, no look-ahead |
| Lab testing | Phase I — fast backtest, small slice |
| Animal trials | Phase II — full history, realistic costs |
| Human trials | Phase III — walk-forward, Monte Carlo, PBO, regime, stress |
| FDA approval | Human dashboard review |
| Post-market surveillance | Phase IV — paper trading, then monitored live |

The expensive tests happen **last**. Most ideas die early and cheaply.

---

## 2. Problem Statement

Quant research today is bottlenecked not by idea generation but by **reliably distinguishing genuine alpha from overfitting**, and by the fact that institutional knowledge lives in scattered notebooks and human memory.

Concretely, the problems AQRL solves:

- **P1 — Idea throughput.** A human researcher tests a handful of hypotheses per week. Most are never recorded.
- **P2 — Validation rigor.** Ad-hoc backtests silently leak future information, ignore costs, and overfit to a single regime.
- **P3 — Amnesia.** Failed experiments are not stored in a structured way, so the same dead ends get re-explored indefinitely.
- **P4 — No feedback loop.** Live performance rarely flows back into the research process.
- **P5 — Unclear stop rules.** Nobody knows when a live strategy is dead versus merely in a normal losing streak.

---

## 3. Non-Goals

Explicitly **out of scope**, permanently or for now:

- ❌ **Automatic promotion to live trading.** A human approves before paper trading and again before live capital. Never negotiable.
- ❌ Beating Renaissance/Two Sigma at their own game. We compete on research *workflow*, not on data budget or headcount.
- ❌ HFT / latency-sensitive execution. The lab may study sub-minute data but will not compete on speed.
- ❌ Options and exotic derivatives in v1.
- ❌ Claude performing mechanical work (downloading PDFs, renaming files, moving data). Claude reasons; Python does everything else.
- ❌ Free-form formula invention by the LLM. Strategies are composed from a vetted operator library.
- ❌ Building for 100,000 experiments/day on day one. The architecture must *allow* that scale; v1 runs on a laptop.

---

## 4. Success Criteria

### 4.1 The v1 KPI — a single question

> **Can AQRL independently discover one statistically valid strategy that a human did not explicitly design?**

If it does that once, the concept is proven. If it does it repeatedly across different markets, we have something rare. Everything after that is a horizontal-scaling engineering problem, not a redesign.

### 4.2 What we do *not* measure

We do **not** optimize for "number of strategies generated" or "number of backtests run." Generating 10,000 backtests is trivial and actively harmful if it inflates the multiple-testing burden without discipline.

### 4.3 What we do measure

| Metric | Why it matters |
|---|---|
| Hypotheses generated | Input volume |
| Experiments completed | Throughput |
| **Experiments reproducible from stored artifacts** | Scientific integrity — target 100% |
| Strategies passing all Phase III gates | Real funnel yield |
| Strategies surviving paper trading | Forward-evidence yield |
| Strategies still healthy at 3 / 6 / 12 months live | The only metric that pays |
| Novel lessons added to knowledge base | Is the lab learning? |
| **Repeat-failure rate** (ideas re-tested that memory should have killed) | Is memory actually working? Target → 0 |
| Cost per credible discovery | Efficiency of the funnel |
| **False discovery rate (null-world)** | See §4.5 — the headline integrity number |

### 4.5 The headline integrity metric — null-world false discovery rate

**The single most important number the laboratory produces about itself.**

Run the entire loop on data with **no alpha by construction** — permuted returns, block-bootstrapped noise, or synthetic paths with matched volatility and fat tails. Count how many "statistically valid discoveries" it reports.

- **0 discoveries** → the pipeline is honest; real results can be trusted.
- **12 discoveries** → the pipeline manufactures roughly 12 findings from nothing, and every real discovery it has ever produced is suspect.

This is the only way to know whether the machine works. It is a permanent regression test: re-run it after every change to `evaluate.py`, every new profile, every change to the scoring rule.

**No real-data result may be trusted before the null-world FDR has been measured and driven low.** This is Milestone 0.

### 4.6 Research portfolio allocation

Borrowed from pharma R&D. The Research Scientist's hypothesis budget is split:

- **70%** — incremental improvement of proven strategy families
- **20%** — adapting ideas across markets/asset classes
- **10%** — high-risk unconventional ideas

---

## 5. Users & Roles

| Role | Who | Interaction |
|---|---|---|
| **Research Director** | You (human) | Sets goals and budgets, reviews the dashboard, approves paper→live promotions, kills strategies |
| **The Laboratory** | 5 agent roles | Runs autonomously between human gates |
| **Reviewer (future)** | Additional humans | Read-only dashboard access, can comment on promotion candidates |

The human is a **gatekeeper and goal-setter, not an operator.** If the human is doing routine work, the design has failed.

---

## 6. The Five Agents

Fixed as of the 2026-07-27 session. Agents 4 and 5 are **separate** — they answer different questions and merging them overloads Agent 4 as the archive grows.

| # | Role | Single question it answers | Output |
|---|---|---|---|
| **A1** | Research Scientist | "What is worth investigating next?" | Strategy Spec |
| **A2** | Quant Engineer | "How do I implement this correctly?" | Executable strategy code |
| **A3** | Research Reviewer | "What did we learn, and what is the next experiment?" | Research Plan, or a verdict |
| **A4** | Promotion Committee | "Should this advance to the next stage?" | Promotion decision |
| **A5** | Knowledge Manager | "What did the laboratory learn from this?" | Knowledge entries + graph updates |

### 6.1 Critical separation of concerns

**A3 produces a research plan, never code.** It says "increase the ATR multiplier" or "replace SMA with JMA" or "add a volatility regime filter" — A2 decides how to implement it. This keeps the scientist separate from the engineer, and means A3's reasoning is reviewable in plain language.

**A4 sees the entire research history**, not just the final strategy. A strategy that reached Sharpe 2.1 after 47 iterations of tinkering is a very different object from one that hit 1.6 on the second try, and A4 must be able to tell them apart — that iteration count *is* the overfitting signal.

**A5 works across experiments, not within one.** Its value emerges from noticing that experiments #25, #193 and #6201 all failed for the same reason, and promoting that into a global rule: *"Avoid ATR multipliers above 3.0 in this strategy family."*

---

## 7. The Closed Loop

```
Knowledge Base
      │
      ▼
Generate hypotheses  ← A1
      │
      ▼
Implement strategy   ← A2
      │
      ▼
Evaluate (evaluate.py)
      │
      ▼
Critique             ← A3 ──┐
      │                     │  iterate (evidence-based stop)
      └─────────────────────┘
      │
      ▼
Promote?             ← A4
      │
      ▼
Record learning      ← A5
      │
      ▼
Better hypotheses ───↺
```

### 7.1 The loop only improves if it receives new information

This is the most important caveat in the entire design. A closed loop running on a fixed dataset with a fixed operator library will hit diminishing returns and begin rediscovering variants of the same ideas — while *appearing* busy. Required inputs:

- New market data (daily)
- New academic literature (continuous ingestion)
- New operators added to the library (human + agent proposed)
- Live and paper trading results fed back
- Research questions generated by the lab's own failures

### 7.2 The Curiosity Engine

The lab must not passively consume external knowledge. When an experiment fails in a specific way, that failure **generates a targeted search**:

```
Observation: "Momentum strategies fail in high-volatility regimes"
      ↓
Auto-generated research question: "Find literature on volatility-adaptive momentum"
      ↓
Queued for the external collectors
      ↓
New knowledge → better hypotheses next cycle
```

This is the difference between a retrieval system and a researcher.

---

## 8. Knowledge Architecture

Two knowledge bases with fundamentally different trust levels.

### 8.1 Internal Knowledge — the lab's own memory

**Ground truth**, because we generated it. Append-only; never overwritten. Contains hypotheses, code versions, parameters, market/timeframe, all evaluation results, failure reasons, live results, and lessons.

This is the moat. Not Claude Code — anyone can use Claude Code. The moat is a large, well-curated experiment database plus a validation pipeline that reliably kills weak ideas.

### 8.2 External Knowledge — the world's memory

Four layers:

| Layer | Contents |
|---|---|
| Research Knowledge | Papers, books, indicators, validation techniques |
| Market Knowledge | Current regime stats, volatility, correlations, liquidity |
| Software Knowledge | New libraries, faster algorithms, better optimizers |
| Infrastructure Knowledge | Better testing/validation/risk methodology |

**Store knowledge, not documents.** A 40-page paper becomes a structured record: `{paper_id, core_idea, category, applicable_markets, strength, weakness, implementation_difficulty, proposed_experiments, confidence}`. Claude reads any given paper exactly once, ever.

**Claude does not browse the web.** Ordinary Python collectors gather and pre-process; Claude consumes prepared knowledge.

---

## 9. Strategy Lifecycle & Promotion Rules

### 9.1 Stages

```
Idea → Spec → Code → Backtest → Statistical Validation → Stress Testing
  → [HUMAN GATE] → Paper Trading → [HUMAN GATE] → Live 1–5% → Scale Up → Full
                                                        ↓
                                              Monitor → Retire / Return to research
```

### 9.2 The iteration stop rule (A2↔A3 loop)

**Evidence-based, not a fixed count.** Stop when *any* of:

- All promotion criteria are met
- No meaningful improvement for *N* consecutive iterations (plateau)
- Iteration/compute budget exhausted
- A3 judges further modification unlikely to produce a robust result

A hard iteration cap exists as a backstop only. **Iteration count is recorded and passed to A4 as an overfitting signal** — more iterations means more multiple-testing burden, which must be reflected in the deflated Sharpe calculation.

### 9.3 Paper trading promotion — trades, not calendar

Time is only a proxy. What we need is **enough independent observations**. A strategy trading 5×/year needs far longer than one trading 5×/day.

| Strategy type | Minimum trades | Better | Excellent |
|---|---:|---:|---:|
| High frequency | 5,000 | 20,000 | 100,000+ |
| Intraday | 500 | 1,000 | 2,000+ |
| Swing | 100 | 250 | 500+ |
| Position | 50 | 100 | 200+ |

Trade count alone is insufficient. **All** of the following are required for promotion:

- ✅ Minimum trade count reached
- ✅ Live Sharpe / profit factor / max DD within acceptable deviation of validated expectations
- ✅ Coverage of multiple regimes (trending, sideways, high-vol, low-vol)
- ✅ No abnormal slippage, missed fills, or execution errors
- ✅ Statistical health checks show behavior consistent with the validated model

The question is never *"has it paper traded for 6 months?"* It is **"have we collected enough high-quality evidence to justify risking real capital?"**

### 9.4 Live monitoring — the health question

Never decide on drawdown alone. The question is not *"has this strategy lost money?"* but:

> **"Is this strategy still behaving like the strategy we originally validated?"**

A good strategy can lose money for months and remain healthy. A bad strategy can make money for months on luck while its underlying characteristics have already broken.

Monitored dimensions: performance, statistical behavior (win rate, avg trade, loss distribution vs expected), market regime context, and execution quality.

| Level | Condition | Action |
|---|---|---|
| 🟢 Green | Within historical expectations | Continue |
| 🟡 Yellow | Deteriorating but statistically plausible | Reduce size, investigate |
| 🟠 Orange | Multiple warning signals | Pause new capital, revalidate on recent data |
| 🔴 Red | Live behavior no longer matches validated model | Stop trading, return to research pipeline |

A 20% drawdown may be perfectly acceptable if validation showed a 15–25% range. A 100% drawdown means risk limits and kill switches failed long before — **portfolio- and strategy-level hard limits must make that state unreachable.**

---

## 10. Foundational Principles

1. **Every decision answers "why did you do this?"** Every hypothesis, code change, promotion, and retirement carries a traceable, data-backed explanation. This buys reproducibility, auditability, faster debugging, and the ability to improve the system rather than treat it as a black box.
2. **The lab is self-critical, not just self-creative.** For every agent proposing ideas, there is machinery whose only job is to falsify them — look for leakage, look-ahead bias, cost sensitivity, regime dependence.
3. **Knowledge is the central asset**, not any individual strategy.
4. **Failures are data.** A rejected experiment must answer: why did it fail, was it overfit, was it regime-specific, did costs kill it, was it too correlated with something we already have?
5. **Every experiment generates the next experiments.** A lab notebook entry that ends without proposed follow-ups is incomplete — that is what makes the system self-propelling.
6. **Comparability is sacred.** One evaluation engine, versioned, profile-driven. See TRD §4.
7. **Markets adapt.** No lab produces a constant stream of durable alpha. The value is that it keeps searching as edges decay.

---

## 11. Scope — Markets & Data

| Asset class | Coverage | Notes |
|---|---|---|
| US Equities | 2000–present | Survivorship bias handling required |
| US Indices | 2000–present | |
| Indian Equities | 2000–present | Primary focus; existing JMA+ATR work lives here |
| Crypto | Exchange inception–present | 24/7, funding rates |
| Commodities | 2000–present | Contract roll handling |
| Forex | 2000–present | Spread-driven costs |

---

## 12. Existing Assets

The repository already contains reusable pieces (see `README.md`):

| Asset | Maps to |
|---|---|
| `ai/indicator_extractor.py`, `ai/summarizer.py` | External knowledge extraction pattern (currently sourced from Drive notebooks) |
| `utils/indicator_registry.py` | Seed of the Operator Library |
| `ai/strategy_generator.py` | Seed of A1 |
| `engine/backtester.py`, `jobs.py`, `registry.py`, `tradebook.py` | The "hands" — execution substrate |
| Existing JMA + ATR research, walk-forward, Monte Carlo, robustness scoring | Seed of the validation battery |

**Gap analysis:** the current repo is a one-shot `notebooks → indicators → strategies → tradebooks` pass. Missing: `evaluate.py` with the full statistical battery, the experiment database, the A2↔A3 iteration loop, the scheduler, and the knowledge memory. It is a partial A1+A2 skeleton with no loop.

---

## 13. The Search Objective

### 13.1 Two phases, in strict order

The objective the loop optimises **changes once** in the life of the laboratory.

| Phase | Objective | When |
|---|---|---|
| **Phase A** | *"Find one strategy that is tradeable, risk-controlled and robust."* | Now, until the first strategy completes paper trading |
| **Phase B** | *"Find a good strategy that makes money when the ones I already run don't."* | After Phase A completes |

You cannot build a portfolio from zero strategies. Phase A is the v1 KPI (§4.1) and nothing about Phase B replaces it.

### 13.2 Satisficing, not maximising ★

**"Find the best possible strategy" is the wrong instruction**, even in Phase A.

If the objective is *best*, the loop never stops. It keeps grinding for a higher number, and every extra attempt is another trial burned against the same data. That is how a strategy ends up excellent on history and mediocre in reality.

Instead: **write the acceptance bar down before the search begins**, and take the *first* strategy that clears it and holds.

The bar lives in `program.md` and is decided in advance:
- Minimum out-of-sample score
- Maximum drawdown (survivable financially and emotionally)
- Minimum trade count
- Maximum complexity (number of rules/filters)
- Must survive costs at 2× assumed level

The strategy found on attempt 400 is mostly better *at fitting history*. The one that clears a pre-set bar on attempt 30 is more likely to survive live. Fewer trials means less selection bias, which means the out-of-sample number remains believable.

The loop may continue running afterwards, but a later, higher score must not replace an already-passing strategy unless it wins by a wide margin **on data the search never touched**.

### 13.3 Ranked acceptance criteria

Modelled directly on the reference project's primary/secondary/tertiary structure:

1. **Primary** — the honest score (TRD §4A)
2. **Secondary** — a resource constraint: capacity, turnover, or capital efficiency
3. **Tertiary** — **simplicity.** Where two strategies score alike, the simpler wins

Simplicity is a *scored criterion*, not a matter of reviewer judgment. A strategy with 7 filters must beat one with 2 filters by a real margin, not a hair. Complexity is one of the few reliable predictors of overfitting, so it must cost something.

### 13.4 Why diversification is an anti-overfitting device, not a lowering of standards

Phase B is frequently misread as "accept worse strategies." It is not. There are two distinct questions:

**Question 1 — "Is this tradeable at all?"** A pass/fail floor. Non-negotiable: risk controlled, survives out-of-sample, survives realistic costs, actually executable, drawdown within limits, and the mechanism is understood. A failing strategy is rejected. Diversity never rescues it.

**Question 2 — "Given what I already run, is this worth adding?"** Asked *only* of strategies that already cleared Question 1.

The arithmetic that motivates Phase B: two strategies each at Sharpe 1.0 that are genuinely uncorrelated combine to roughly 1.41; three to 1.73; four to 2.0. A strategy at **Sharpe 0.7 that profits when the existing one bleeds is worth more** than one at Sharpe 1.3 that profits at the same time. A strategy's value is measured against the book, not in isolation.

The same logic applies to drawdown. Per-strategy limits are unchanged, but the number that matters is **portfolio** drawdown — and two strategies whose bad periods occur at different times produce a combined drawdown smaller than either alone. Diversification is the strongest drawdown control available, stronger than tightening stops on a single strategy.

**The overfitting argument is the decisive one.** Trend following fails in sideways markets; that is inherent to the logic, not a defect. The natural response — adding filters and regime detectors until it performs well in every historical period — does not fix the weakness. It memorises where the weakness occurred in *this* history. The honest alternative is to accept the specialisation and find a *different* strategy for that regime. "Works everywhere" is usually a fitted illusion.

### 13.5 Fake diversification

Guarded against explicitly. These are **not** diverse — they are the same bet in different clothes, and their correlation rises precisely during crises:

- The same strategy across 20 instruments
- The same logic at different lookback lengths
- Trend following on NIFTY and on Bank NIFTY
- Trend following on equities and on commodities

Genuine diversity is difference in **logic**: trend vs mean reversion, long vs short holding period, volatility breakout vs volatility selling, price-based vs volume-based vs cross-sectional.

The test is not whether it feels different. The test is whether the return series actually move independently — measured, including correlation during each strategy's worst months.

---

## 14. Design Reference — karpathy/autoresearch

The structural template for v1. What it is: three files — `prepare.py` (fixed, agent may read but never edit), `train.py` (**the only file the agent edits**), and `program.md` (instructions, **edited by humans**). Fixed 5-minute wall-clock budget per experiment. Metric is `val_bpb`. Improved → keep the commit; worse or equal → `git reset`. ~100 experiments overnight. Agent cannot modify the evaluation harness or install dependencies. Ranked criteria with simplicity as tertiary. Once running, the agent does not stop to ask the human whether to continue.

### 14.1 What we adopt

- The **file structure and its permissions boundary** (TRD §2A)
- **Evaluator isolation enforced structurally**, not by instruction
- **One fixed invariant** that makes all results comparable
- **Simplicity as a ranked scoring criterion**
- **`program.md` edited by humans, never by the agent** — the honest version of "the lab learns"
- **No human interruption of the research loop** (gates exist only at paper and live)
- **Explicit keep / discard / crash status** on every experiment

### 14.2 What does not transfer, and why it matters

**His metric is nearly unhackable; ours is trivially hackable.**

`val_bpb` is a held-out likelihood. Running 100 experiments and keeping the best is epistemically sound — the held-out set was never trained on. Running 100 backtests and keeping the best Sharpe is a selection-bias failure: with enough attempts, noise produces a beautiful equity curve.

So the reference's encouragement toward high throughput and keep-if-improved makes our integrity machinery **more** necessary, not less:

- **The vault** (TRD §8A.2) — data the loop physically cannot read
- **Null-world calibration** (§4.5) — measuring how often we invent discoveries
- **Satisficing** (§13.2) — stopping early rather than searching for the maximum

Adopt the throughput. Do **not** adopt the keep/discard rule unmodified. In his setting, improvement-on-metric is evidence. In ours, it is a hypothesis.

---

## 15. Open Questions

Tracked here until resolved in a session, then moved into the body of the docs.

- [ ] **★ What is the honest score — our `val_bpb` equivalent? Blocks everything else (TRD §4A)**
- [ ] The numeric acceptance bar for §13.2, written before the search begins
- [ ] Which specific statistical tests are gating (hard fail) vs advisory in Phase III?
- [ ] Numeric thresholds for each promotion gate (deflated Sharpe floor, PBO ceiling, MC 5th-percentile floor)
- [ ] How is "genuinely different" measured for Phase B admission (correlation ceiling, and over which window)?
- [ ] Broker/data-feed choice for paper trading per market
- [ ] Correlation ceiling for admitting a new strategy to the live portfolio
- [ ] Whether A1 hypothesis generation is scheduled (nightly batch) or purely event-driven
- [ ] Capacity/AUM modelling — at what point does liquidity invalidate a backtest
- [ ] Human review SLA — how long may a candidate sit in the dashboard queue

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial document. Vision, 5-agent architecture, promotion rules, health monitoring, portfolio allocation, success criteria captured from architecture sessions. |
| 2026-07-27 | Reconciled against karpathy/autoresearch (§14). Added null-world false discovery rate as the headline integrity metric (§4.5), the two-phase search objective, satisficing over maximising, ranked acceptance criteria with a simplicity penalty, and the diversification-as-anti-overfitting argument (§13). |
