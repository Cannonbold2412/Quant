# Three-Phase Framework: From Single-Strategy Research to a Strategy Portfolio

## The problem

Running one strategy through a strict evaluation harness (walk-forward, honest costs,
robustness gates) and tuning it until its own standalone numbers look good enough to trade
by itself conflates two different jobs: proving an edge is real, and maximizing
risk-adjusted return. A single strategy squeezed to be "the whole product" tends toward
fragile over-optimization (the FY2024-26 decay problem is a symptom of this). The fix is
structural: split the work into three phases with different fitness functions.

## Phase 1 — Edge Discovery

Per strategy. Prove the edge is real: honest transaction costs, walk-forward
out-of-sample testing, robust to parameter perturbation, no lookahead. The bar is
**"is this real and stable,"** not "is this good enough to trade alone" — a strategy with
a modest but genuine, robust edge can still be very valuable inside a portfolio even if it
would never pass as a standalone product. Roughly what `original/evaluate.py` already
does, recalibrated off the "must stand alone" bar.

## Phase 2 — Portfolio Construction

Given a pool of Phase-1-passed strategies, decide which combination actually earns its
place:

- **Marginal-contribution test** — does adding this strategy raise the *combined*
  portfolio's Sharpe / cut its drawdown, not just its own solo numbers.
- **Diversification check** — real diversification needs genuinely different kinds of
  edge (different signal logic, different holding horizon, ideally different
  instruments), not parameter variants of the same idea, which stay highly correlated.
- **Stress/tail test** — correlations that look low in calm periods often converge in a
  crash, exactly when diversification is needed most; check joint behavior under stress,
  not just an average correlation.
- **Capital allocation rule** — how much risk/capital each strategy gets (equal-weight,
  risk-parity, etc.), and how contention is resolved when two strategies want the same
  stock/capital at once.

Output: the set of strategies in the book, and their weights.

## Phase 3 — Portfolio-Aware Refinement

The ongoing tuning loop after strategies are in the book: improving individual
strategies, re-weighting, retiring strategies whose edge has decayed, folding in newly
discovered Phase-1 passes. The governing rule: **every change is judged by re-running the
whole portfolio**, not the one strategy's own numbers — a tweak that raises a strategy's
solo Sharpe by making it trade more like an existing strategy is a portfolio loss even
though it looks like a local win. Giving this its own phase (rather than letting it
happen informally inside Phase 1 or Phase 2) is what stops it from sliding back into the
old single-strategy over-optimization trap. Tuning aimed at cutting a strategy's own tail
risk/drawdown tends to still help the portfolio (correlated crashes are driven by each
component's own worst days); tuning aimed at chasing more return on the same historical
track is the risky kind — same curve-fitting failure mode as before, just hidden inside
one component of a bigger book.

## Cold Start — Getting From One Strategy to a Lab

The three phases assume a pool of strategies to work with, but the pool starts empty (or
with just one). "No portfolio yet" isn't a blocker — Phase 2 can't run without at least two
Phase-1-passed candidates to compare, so at the very start the only job is producing a
second candidate. Concretely, in order:

1. **Freeze the existing strategy as Candidate #1.** The JMA-crossover work in `original/`
   is already validated under a strict Phase-1-style harness (`evaluate.py`) — real edge,
   walk-forward, honest costs, robustness gates. It doesn't stop counting just because
   Phase 3 says stop chasing its solo Sharpe further. Treat it as done for Phase 1
   purposes and redirect further effort away from it.

2. **Generalize the evaluation harness instead of rebuilding it.** `original/evaluate.py`
   is already mostly strategy-agnostic — it drives any module through three hooks
   (`curate_universe()`, `generate_signals()`, `exit_params()`) and keeps the walk-forward
   protocol, cost model, capital rules, and gates frozen and shared. That's the right
   shape for a lab: one common, frozen Phase-1 evaluation engine that every new candidate
   plugs into, so results stay comparable across strategies — Phase 2's correlation and
   marginal-contribution tests only mean something if every candidate was judged on the
   identical protocol.

3. **Give each new candidate its own isolated lane.** Same pattern as `original/` — a
   dedicated branch/folder per strategy, its own `strategy.py`, its own `results.tsv`
   experiment log — so candidates don't collide or contaminate each other's fitting, and
   each can be independently frozen once it clears Phase 1.

4. **Generate Candidate #2 deliberately unlike Candidate #1.** A second trend-follower
   (another JMA-period variant) buys nothing — it's the same edge wearing a costume. The
   candidate-generation step (autonomous loop or hand-picked) should target a genuinely
   different mechanism family: mean-reversion, stat-arb/pairs, volatility-regime-based,
   cross-sectional ranking, different timeframe, different instrument class. `RESEARCH_PAPERS.md`
   already has scored ideas that are mechanistically distinct from JMA crossover and can
   seed this without starting from a blank page.

5. **Track candidates at the strategy level, not just the experiment level.**
   `results.tsv` logs tuning attempts *within* one strategy. The lab needs one layer above
   that: a small registry (one row per distinct strategy candidate — mechanism family,
   Phase-1 pass/fail, headline honest metrics) so the pool is visible at a glance once it
   grows past one.

6. **Two Phase-1 passes is the trigger for Phase 2.** There's no reason to build
   correlation/marginal-contribution/stress-test tooling before a second candidate exists
   to test it against — prototype Phase 2 methodology on the first pair, then generalize
   as the pool grows.

## Summary

**Find edge → decide what belongs together and how much of each → keep improving it
without breaking the diversification that makes it work.**

With one strategy today: freeze it as Candidate #1, keep the evaluation harness shared and
frozen, and put all new effort into sourcing a mechanistically different Candidate #2 —
the pool, not the machinery, is the current bottleneck.
