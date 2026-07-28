# AQRL — Autonomous Quantitative Research Laboratory

> **Status: design phase. No implementation yet.**
> This repository currently contains architecture documents only. Nothing has been built.

---

## What this is

Not an AI hedge fund. A **closed-loop scientific discovery system** for quantitative trading — a machine that generates hypotheses, implements them, tests them adversarially, records why they failed, and uses that record to generate better hypotheses.

> An AI hedge fund says: *"We use AI to trade."*
> AQRL says: *"We build machines that discover and validate financial knowledge."*

The distinction is the whole point. A good strategy has a 2–5 year edge. A good research engine keeps finding strategies for decades. **Strategies are outputs of the system; the system is the asset.**

The governing analogy is drug discovery: millions of candidates, cheap screens first, expensive trials last, most die early, and *why* each one died is documented as carefully as why one survived.

---

## The documents

| Document | Answers |
|---|---|
| **[PRD.md](PRD.md)** | Why. Vision, the five agents, promotion rules, live health monitoring, success criteria, the search objective |
| **[TRD.md](TRD.md)** | How. Execution model, the honest score, walk-forward scheme, performance, adversarial integrity, safety |
| **[Backend-Schema.md](Backend-Schema.md)** | Data. Tables, columns, enums, and the queries the schema exists to answer fast |
| **[App-Flow.md](App-Flow.md)** | Sequences. Every flow end to end, error paths, and the traceability chain |
| **[Implementation_Plan.md](Implementation_Plan.md)** | Order. Stages, milestones, risk register |
| **[UI-UX-Brief.md](UI-UX-Brief.md)** | Interface. The dashboard, built last |

Each carries an open-questions section and a changelog. They are living documents, updated after every design session.

---

## Core decisions, settled

**The honest score** — the single float that drives every keep/discard decision:

```
score = SR_oos − 2·SE(SR) − SR*(N_trials)
```

The deflated lower bound on out-of-sample Sharpe. *What Sharpe can we be confident is real, after accounting for how few trades we have, how ugly the tails are, and how many things we already tried?* Every other metric is computed and stored, but none of them drive the loop. → TRD §4A

**Walk-forward** — rolling window, test period fixed at 1 year, train period configurable at 1/2/3 years (chosen once per family, default 1). All folds concatenated into one out-of-sample series. Both the scheme and the train length are fixed per campaign and hashed into provenance as `wf_config_hash`, because *trying several schemes or train lengths and reporting the best is a multiple-testing channel the deflated Sharpe cannot see* — and since `evaluate.py` is off-limits to the agent, this can never be an iteration lever in the first place. → TRD §4A.2f–2i

**The bar and the score are separate.** A pass/fail bar — minimum trades, maximum drawdown, breadth, 2× cost survival, complexity cap — runs before any score is computed. Drawdown gates but does not rank; a worst-moment statistic is too noisy to rank on. → TRD §4A.3a

**Satisficing, not maximising.** Take the *first* strategy that clears a pre-registered bar, not the best after 500 tries. The strategy found on attempt 400 is mostly better at fitting history, and out-of-sample data is a consumable resource. → PRD §13.2

**Storage** — SQLite for metadata, git for strategy code, `experiments.code_commit` linking them. → TRD §2A.4

---

## The three integrity mechanisms

These are what make automated search in markets defensible. **None are optional.**

**1. Null-world calibration.** Run the entire loop on data with no alpha by construction — permuted returns, block bootstrap, synthetic paths. Count the "discoveries." Zero means the pipeline is honest. Twelve means it invents twelve findings from nothing, and every real result it has ever produced is suspect. That count is the false discovery rate, and it is **Milestone 0** — no real-data result is trusted before it is measured and driven low.

**2. The vault.** A span of years, instruments, or an entire market that the loop has **no read path to**. Not "should not" — cannot. Opened once per strategy family at promotion, logged, against a lifetime budget. Every other protection depends on honestly counting trials; once an LLM generates hypotheses influenced by memory of past results, that count is unknowable. This is the one defence that doesn't depend on counting anything.

**3. Evaluator isolation.** The agent can neither read nor edit `evaluate.py`, and `data.py` is read-only. Reward hacking is a certainty, not a risk — give an agent a scoring function and enough iterations and it will optimise the scorer rather than the market. Structural, not procedural.

→ TRD §8A

---

## What gets built first — nanoAQRL

Five files, modelled on the design reference. The five-agent architecture, job queue, knowledge graph and full schema are **later stages**, added on felt need.

| File | Contents | Agent permission |
|---|---|---|
| `data.py` | Snapshots, calendars, cost models, universe | read only |
| `strategy.py` | Signal logic, entries, exits, sizing | **the only writable file** |
| `evaluate.py` | Scoring harness + hard bar | **no read, no write** |
| `program.md` | Instructions and the acceptance bar | human-edited only |
| `results.tsv` | `commit \| score \| n_trades \| status \| description` | append only |

```
edit strategy.py → commit → run evaluate.py
     → hard bar fails?  discard, no score computed
     → bar passes?      honest_score
     → improved?  keep the commit
     → worse?     git reset
     → append one row, repeat unattended
```

Statuses are exactly three: `keep` · `discard` · `crash`. Every experiment gets one.

→ TRD §2A, App-Flow §1A

---

## Order of work

Steps 1–4 are the real work. Step 5 is small. **That ratio is the point.**

1. **Implement the honest score** — rolling walk-forward, purged, embargoed, 2× costs, one float out
2. **Build `evaluate.py`** around it, with the hard bar enforced *inside* it
3. **Run the null-world test.** Prove the scorer doesn't hallucinate discoveries. Fix and repeat
4. **Build the vault** before the loop ever touches real data
5. **Write `program.md` and `strategy.py`**, wire the keep/reset loop
6. **Run it one night. Read every row of `results.tsv` by hand**
7. **Improve `program.md`** from what you saw. Repeat for weeks

→ Implementation_Plan §1A

---

## The dashboard, and what ships before it

**The decision layer — Decisions, Health, Pipeline, Laboratory, Knowledge — is built last, at Stage 12,** once the pipeline reliably produces candidates worth reviewing.

**Observability ships much earlier, at Stage 4a** — a live feed of which agent is doing what, and a read-only browser over the database — because once a scheduler is dispatching to more than one agent, a terminal alone stops being enough to see what's happening. It exists to debug the machine, not to approve capital, and it stays structurally separate from the decision screens.

The Pipeline screen itself uses **deployment** (`strategy × market × mode`) as its atomic unit, grouped by strategy or by market via a toggle — not a fixed hierarchy, since the two answer different questions (how research accrues, vs. where risk concentrates). Paper and live are stages on that one lifecycle track, not separate tabs. → UI-UX-Brief §7, §7a

---

## Success criteria

**M0 — null-world FDR measured and low.** Gates everything. A discovery from an uncalibrated pipeline is not a discovery.

**M6 — the v1 KPI:**

> Can AQRL independently discover one statistically valid strategy that a human did not explicitly design?

Once, and the concept is proven. Repeatedly across markets, and it's something rare.

**What is deliberately not measured:** number of strategies generated, or number of backtests run. Generating 10,000 backtests is trivial and actively harmful if it inflates the multiple-testing burden without discipline.

---

## Safety

- **Two mandatory human gates** — research→paper and paper→live. No code path bypasses them.
- **Agents hold no trading credentials.** They recommend; only a separate execution service, gated on a human-approved record, can act.
- **Hard risk limits live outside strategy logic**, so a total loss is structurally unreachable.
- Live capital ramps in stages (1–5% → scale up), never straight to full allocation.

---

## Design reference

[karpathy/autoresearch](https://github.com/karpathy/autoresearch) — three files, a fixed time budget, one metric, keep-or-reset on git, and an agent that runs unattended overnight.

**What transfers:** the file structure and its permission boundary, evaluator isolation enforced structurally, one fixed invariant for comparability, simplicity as a ranked criterion, `program.md` edited by humans, and no human interruption of the research loop.

**What does not:** his metric is nearly unhackable; ours is trivially hackable. `val_bpb` is a held-out likelihood, so running 100 experiments and keeping the best is sound. Running 100 backtests and keeping the best Sharpe is a selection-bias failure — with enough attempts, noise produces a beautiful equity curve. Adopt the throughput; do not adopt the keep/discard rule unmodified.

→ PRD §14

---

## Open — owned by the human

Numeric and strategic choices that shape the build:

- The acceptance bar values — min trades, max out-of-sample drawdown, breadth, complexity cap
- The `z` multiplier on the uncertainty haircut — `2.0` (~97.5% one-sided) or `1.65` (~95%)
- **Whether the agent tunes parameters per fold or writes fixed-parameter strategies** — changes what walk-forward is actually testing, and how `evaluate.py` must be built
- Vault composition and per-family peek budget
- First fully-supported market × timeframe profile

---

## Prior prototype

An earlier prototype — Drive notebook ingestion, indicator extraction, a backtest engine, tradebook generation — was removed from the working tree to give the build a clean start.

**Nothing is lost.** It remains in history at `d08e812`:

```bash
git show d08e812:engine/backtester.py     # view a file
git checkout d08e812 -- engine/           # restore a directory
git checkout d08e812 -- .gitignore        # you will want this back before the first run
```

Implementation_Plan §17 maps each prototype component to the stage that reuses it. They are reference implementations to borrow from, not a codebase to extend.
