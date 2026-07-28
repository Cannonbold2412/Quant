# AQRL — Autonomous Quantitative Research Laboratory

> **Status: Stages 0–1 built.**
> `nanoaqrl/` is the working research loop (Stage 0); `aqrl/` is the foundation layer it now runs on (Stage 1). Stages 2–13 remain design only.
>
> ```bash
> pip install -e .            # Python 3.11+
> aqrl db migrate             # create the schema
> aqrl profile show nse_equity --timeframe daily
> pytest                      # 179 tests
> ```

---

## What this is

Not an AI hedge fund. A **closed-loop scientific discovery system** for quantitative trading — a machine that generates hypotheses, implements them, tests them adversarially, records why they failed, and uses that record to generate better hypotheses.

> An AI hedge fund says: *"We use AI to trade."*
> AQRL says: *"We build machines that discover and validate financial knowledge."*

The distinction is the whole point. A good strategy has a 2–5 year edge. A good research engine keeps finding strategies for decades. **Strategies are outputs of the system; the system is the asset.**

The governing analogy is drug discovery: millions of candidates, cheap screens first, expensive trials last, most die early — and *why* each one died is documented as carefully as why one survived.

---

## The documents

| Document | Answers |
|---|---|
| **[PRD.md](PRD.md)** | Why. Vision, the six agents, promotion rules, health monitoring, the search objective |
| **[TRD.md](TRD.md)** | How. nanoAQRL, the honest score, walk-forward protocol, performance, adversarial integrity, safety |
| **[Backend-Schema.md](Backend-Schema.md)** | Data. Every table and column, plus the ten queries the schema exists to answer fast |
| **[App-Flow.md](App-Flow.md)** | Sequences. All thirteen flows end to end, error paths, the traceability chain |
| **[Implementation_Plan.md](Implementation_Plan.md)** | Order. Fourteen stages, nine milestones, risk register |
| **[UI-UX-Brief.md](UI-UX-Brief.md)** | Interface. What ships early, what ships last, and why |

Each carries an open-questions section and a changelog.

---

## Core decisions, settled

**The honest score** — the single float that drives every keep/discard decision:

```
score = SR_oos − 1.65·SE(SR) − SR*(N_trials)
```

The deflated lower bound on out-of-sample Sharpe. *What Sharpe can we be confident is real, after accounting for how few trades we have, how ugly the tails are, and how many things we already tried?* Every other metric is computed and stored, but none of them drive the loop. → TRD §7

**The pre-registered bar:** min score **0.50** · max out-of-sample drawdown **15%** (**20% crypto**) · min **100** trades · survives **2× costs**. Fail any item and the run is discarded with no score computed.

**The bar and the score are separate.** A pass/fail bar — minimum trades, max drawdown, breadth, 2× cost survival, complexity cap — runs before any score is computed. Drawdown gates but does not rank; a worst-moment statistic is too noisy to rank on. → TRD §7.5

**Clearing the bar is an immediate, unconditional stop.** The first passing iteration is the last one — enforced in Python before A3 is even asked. This is satisficing made structural: the bar already contains a minimum score, so clearing it already means "good enough by a standard set in advance." → PRD §9.2

**Walk-forward** — rolling, test window **always 1 year**, evaluated at **all three train windows (1/2/3 yr) with the best reported**, purged with embargo ≥ holding period, folds concatenated. Taking the best of three is a selection, so **`N_trials` is multiplied by three** — the deflated Sharpe absorbs it and the haircut grows accordingly. The *scheme* stays fixed and unsearched, because selecting a scheme by result would sit outside the trial count where the haircut cannot see it. → TRD §8

**Parameter tuning is enabled**, run inside each fold on training data only. It does **not** inflate `N_trials` — it never saw the test window, so it is part of the procedure rather than a selection on the reported metric. It raises internal fold overfitting instead, visible in walk-forward efficiency. → TRD §8.6

**Markets and instruments** — Indian equities (NIFTY-50), Indian and US indices, forex, commodities, crypto — traded as cash equity, ETFs, futures, CFDs, spot and perpetuals. **Cost models are keyed on `(market, asset_class)`**, not market alone. NSE cash delivery works out to ~22 bps statutory, **~27–32 bps all-in** — so a swing strategy must clear ~30 bps per round trip just to break even. → TRD §6.3

**Storage** — SQLite for metadata, git for code, `experiments.code_commit` linking them. One repo forever, one branch per strategy, never deleted. Almost nothing merges — except at approval, where clearing a human gate merges the strategy into `deploy/paper` or `deploy/live`, giving both gates a physical, auditable action. → TRD §5

**The Librarian** — a sixth agent, outside the research loop. Reads external documents through one unified pipeline: chunk by structure, extract per chunk, then synthesize across chunks into a few distinct ideas — **one database row per idea, never one per document.** Every row is tagged `evidence_tier = external_claim`: a paper's claim is a candidate worth testing, never a fact. → TRD §12.2

---

## The three integrity mechanisms

These are what make automated search in markets defensible. **None are optional.**

**1. Null-world calibration.** Run the entire loop on data with no alpha by construction — permuted returns, block bootstrap, synthetic paths. Count the "discoveries." Zero means the pipeline is honest. Twelve means it invents twelve findings from nothing, and every real result it has ever produced is suspect. That count is the false discovery rate, and it is **Milestone 0** — no real-data result is trusted before it is measured and driven low.

**2. The vault.** A span of years, instruments, or an entire market the loop has **no read path to**. Not "should not" — *cannot*. Opened once per strategy family at promotion, logged, against a lifetime budget. Every other protection depends on honestly counting trials; once an LLM generates hypotheses influenced by memory of past results, that count is unknowable. This is the one defence that doesn't depend on counting anything.

**3. Evaluator isolation.** The agent can neither read nor edit `evaluate.py`, and `data.py` is read-only. Reward hacking is a certainty, not a risk — give an agent a scoring function and enough iterations and it will optimise the scorer rather than the market. Structural, not procedural.

→ TRD §15

### And a fourth, defending a different threat: data integrity

The three above stop the **agent** fooling us. They cannot stop the **data** fooling us — a perfectly honest scorer running on corrupted inputs will confidently report discoveries that do not exist, and **null-world calibration cannot catch it**, because the null generator inherits the same corrupted assumptions.

| Problem | Status |
|---|---|
| **Unadjusted prices** — a 1:2 split reads as a −50% move | ✅ Mitigated: raw data immutable, corporate actions in a versioned table, **adjustment applied at load time** so a new split never rewrites history |
| **Survivorship** — today's NIFTY-50 projected back to 2000 | ⚠️ Fix chosen (**point-in-time membership**), blocked on collecting price history for all **~100–150 ever-members**, not today's 50 |

**Indian equity results cannot reach live capital until survivorship is resolved.** Index-level research is structurally unaffected and runs in parallel. → TRD §14

---

## What gets built first — nanoAQRL ✅ built

Five files. The five-agent architecture, job queue, knowledge graph and full schema are **later stages**, added on felt need.

| File | Contents | Agent permission |
|---|---|---|
| `data.py` | Snapshots, calendars, cost models, corporate-action adjustment, point-in-time universe | read only |
| `strategy.py` | Signal logic, entries, exits, sizing | **the only writable file** |
| `evaluate.py` | Scoring harness + hard bar | **no read, no write** |
| `program.md` | Instructions and the acceptance bar | human-edited only |
| `results.tsv` | `commit \| score \| n_trades \| status \| description` | append only |

```
edit strategy.py → commit → run evaluate.py
     → bar fails?    discard, no score computed
     → bar clears?   keep the commit — and STOP
     → append one row, repeat unattended
```

Statuses are exactly three: `keep` · `discard` · `crash`. Every experiment gets one.

→ TRD §2, App-Flow §2

---

## Order of work

Steps 1–4 are the real work. Step 5 is small. **That ratio is the point.**

1. **Implement the honest score** — rolling walk-forward, purged, embargoed, 2× costs, one float out
2. **Build `evaluate.py`** around it, with the hard bar enforced *inside* it
3. **Run the null-world test.** Prove the scorer doesn't hallucinate discoveries. Fix and repeat
4. **Build the vault** before the loop ever touches real data
5. **Write `program.md` and `strategy.py`**, wire the loop
6. **Profile, then parallelise across folds** — processes not threads; verify bit-identical determinism
7. **Run it one night. Read every row of `results.tsv` by hand**
8. **Improve `program.md`** from what you saw. Repeat for weeks

→ Implementation_Plan §2

---

## The dashboard, and what ships before it

**The decision layer** — Decisions, Health, Pipeline, Laboratory, Knowledge — is built **last**, at Stage 12, once the pipeline reliably produces candidates worth reviewing.

**Observability ships much earlier**, at Stage 4a — a live feed of which agent is doing what, and a read-only browser over the database. Once a scheduler dispatches to more than one agent, a terminal stops being enough to see what's happening. It exists to debug the machine, not to approve capital, and stays structurally separate from the decision screens.

The Pipeline screen uses **deployment** (`strategy × market × mode`) as its atomic unit, grouped by strategy or market via a toggle. Paper and live are stages on one lifecycle track, not separate tabs. → UI-UX-Brief §0, §8

---

## Success criteria

**M0 — null-world FDR measured and low.** Gates everything. A discovery from an uncalibrated pipeline is not a discovery.

**M6 — the v1 KPI:**

> Can AQRL independently discover one statistically valid strategy that a human did not explicitly design?

Once proves the concept. Repeatedly across markets means something rare.

**What is deliberately not measured:** number of strategies generated, or number of backtests run. Generating 10,000 backtests is trivial and actively harmful if it inflates the multiple-testing burden without discipline.

---

## Safety

- **Two mandatory human gates** — research→paper and paper→live. No code path bypasses them.
- **Agents hold no trading credentials.** They recommend; only a separate execution service, gated on a human-approved record, can act.
- **A4 can reject or defer alone. It can never approve alone.**
- **Hard risk limits live outside strategy logic**, so a total loss is structurally unreachable.
- Live capital ramps in stages (1–5% → scale up), never straight to full allocation.

---

## Design reference

[karpathy/autoresearch](https://github.com/karpathy/autoresearch) — three files, a fixed time budget, one metric, keep-or-reset on git, an agent that runs unattended overnight.

**What transfers:** the file structure and its permission boundary, evaluator isolation enforced structurally, one fixed invariant for comparability, simplicity as a ranked criterion, `program.md` edited by humans, no human interruption of the research loop, and one branch per campaign.

**What does not:** his metric is nearly unhackable; ours is trivially hackable. `val_bpb` is a held-out likelihood, so running 100 experiments and keeping the best is sound. Running 100 backtests and keeping the best Sharpe is a selection-bias failure — with enough attempts, noise produces a beautiful equity curve. **Adopt the throughput; do not adopt the keep/discard rule unmodified.** → PRD §13

---

## Open — owned by the human

Numeric and strategic choices that shape the build:

- The acceptance bar values — min score, max out-of-sample drawdown, min trades, breadth, complexity cap
- **Where to source price history for the ~100–150 ever-members of NIFTY-50.** If genuinely unobtainable, the honest fallback is index-only trading — not quietly reverting to today's 50
- **Whether corporate-actions history is available**, or needs sourcing too. That, not the adjustment code, is the real Stage 1 blocker
- How **breadth** and **complexity** are measured — both sit in the bar but neither has a definition yet
- Vault composition and per-family peek budget
- How `evaluate.py` isolation is enforced in practice — a separate Unix user with `chmod 700` is the cheap option that genuinely blocks `cat`

---

## Prior prototype

An earlier prototype — Drive notebook ingestion, indicator extraction, a backtest engine, tradebook generation — was removed from the working tree to give the build a clean start.

**Nothing is lost.** It remains in history at `d08e812`:

```bash
git show d08e812:engine/backtester.py     # view a file
git checkout d08e812 -- engine/           # restore a directory
git checkout d08e812 -- .gitignore        # you will want this back before the first run
```

Implementation_Plan §19 maps each component to the stage that reuses it. They are reference implementations to borrow from, not a codebase to extend.
