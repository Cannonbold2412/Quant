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
      bar failed?   discard, no score computed
      bar cleared?  keep the commit — and STOP
      append one row to results.tsv, repeat
```

Statuses are exactly three: `keep`, `discard`, `crash`. Every experiment gets
one.

## The bar's dimensions (not its numbers)

The bar checks, in this order: a **minimum trade count**, a **maximum
out-of-sample drawdown**, **survival at stressed costs**, and a **minimum
honest score**. The exact thresholds are pre-registered in `evaluate.py` and
are not disclosed here on purpose — see the note above.

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
- **Stop when the bar is cleared.** Do not keep searching for a higher
  number — the first passing iteration is the last one.
- **Do not pause to ask whether to continue.**

## Data notes

- The instrument served by `data.py` is a **synthetic, index-level proxy** —
  not real NIFTY-50 data. Real Indian-equity data collection (survivorship,
  corporate actions) is still open; do not treat any result here as a claim
  about real markets.
- A span of recent history is inside the vault. `data.py` has no read path to
  it; do not attempt to work around this.
