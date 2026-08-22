# program.md — nanoAQRL operating instructions

> Human-edited only. This is the **only** channel through which the loop
> learns what will be checked, because it cannot read `evaluate.py` (TRD
> §2.3, §2.4). Two things follow from that split:
>
> - **Reveal correctness rules.** Hiding them only causes avoidable failures —
>   they are not gameable.
> - **Hide the scoring formula and its thresholds.** These *are* gameable —
>   knowing the exact haircut invites aiming at it instead of at a real edge.
>
> These instructions reduce the error rate. **They enforce nothing.** P0 in
> `evaluate.py` is the actual enforcement layer, always.

## The loop

```
edit strategy.py → commit → run evaluate.py
      bar failed?   discard
      bar cleared?  keep the commit — the bar rises to this score
      append one row to results.tsv, repeat
```

Statuses are exactly three: `keep`, `discard`, `crash`. Every experiment gets
one.

## The bar's dimensions (not its numbers)

The bar checks, in this order: a **minimum trade count**, a **maximum
out-of-sample drawdown**, **survival at stressed costs**, and a **minimum
honest score**. The exact thresholds are pre-registered in `evaluate.py` and
are not disclosed here on purpose — see the note above.

**The score threshold ratchets.** The first three are fixed floors and never
move. The fourth does: once a strategy has cleared the pre-registered score
once, that experiment's score *becomes* the threshold, and every later
experiment must strictly beat the best score so far. Matching it is not
enough. This is disclosed because it is not a number and not gameable — it is
the shape of the loop, and an agent that did not know it would stop after its
first keep and leave the run half-done.

Two consequences worth stating plainly:

- **Every keep makes the next one harder.** A run that keeps five times has
  taken the maximum of five attempts, and the maximum of many attempts is
  higher than any one of them deserves. `results.tsv` records the whole
  sequence for exactly this reason — the last row is not the honest summary of
  the run, the column of scores is.
- **A discard on `baseline` is not the same failure as a discard on the
  floors.** It means the work was valid and simply not better. Read
  `bar_failed_on` before concluding anything about why an experiment failed.

## Required — anti-look-ahead rules

**Signal timing**
- Every signal is computed from data available at or before bar *t*.
  `evaluate.py` applies the entry lag centrally (position at *t* = signal at
  *t-1*) — do not shift the signal yourself, and never rely on same-bar
  information to decide same-bar action.
- `shift(-n)` is forbidden anywhere, for any reason.

**Statistics and normalisation**
- Rolling statistics only. Never compute mean, standard deviation, z-score or
  percentile rank over the whole series.
- No centred windows (`center=True`).
- Any fitted transform is fitted on training data only.

**Missing data**
- `bfill()` / `fillna(method='backfill')` is forbidden — it pulls the future
  backwards.
- Forward-fill only, and be explicit about why it is safe.

**Price levels**
- No absolute price thresholds. Express rules in ratio or percentage terms —
  an absolute level does not transfer across instruments and will not survive
  back-adjustment.

**Resampling and joins**
- A bar's own close is not known until that bar closes; do not use it to
  decide an action inside the same bar.

**Fitting**
- Parameter tuning happens inside the fold, on the training window only. If
  tuning ever touches the test window, the fold is worthless.

**Verification duty**
- Before committing, state in the commit message which bars each signal reads
  and what its lag is.

## Required — behavioural rules

- **A P0 rejection is a bug in your code, not an obstacle.** Fix the cause.
  Do not restructure code to pass the check while preserving the behaviour.
- **Prefer the simpler strategy** where results are close.
- **Keep going after a keep.** The bar has risen to your own last score; the
  next experiment must beat it. The run ends when it stops improving, not when
  it first succeeds.
- **Do not pause to ask whether to continue.**

## Data notes

- The instrument served by `data.py` is a **synthetic, index-level proxy** —
  not real NIFTY-50 data. Real Indian-equity data collection (survivorship,
  corporate actions) is still open; do not treat any result here as a claim
  about real markets.
- A span of recent history is inside the vault. `data.py` has no read path to
  it; do not attempt to work around this.
