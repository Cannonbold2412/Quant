"""Signal operators — the directional and boolean vocabulary (TRD §11).

Crossovers, thresholds, breakouts, volatility expansion, momentum, mean
reversion, volume confirmation, and the logical combinators that join them.

**Convention.** A *directional* signal is in `[-1, +1]`: −1 short, 0 flat, +1
long. A *boolean* signal is 0 or 1 and is meant for `filter_logic`. Both are
NaN during warm-up so a strategy is never handed a confident-looking 0 for a bar
whose indicators have not converged.

**The lag is not applied here.** `aqrl/research/backtest.py` applies
`position[t] = signal[t-1]` centrally and unconditionally, so an operator must
emit its decision *for* bar `t` using data available *at* bar `t`. Shifting here
as well would double-lag every strategy.

**`and`/`or` are declared commutative**, which is what makes the spec hash treat
`and(a, b)` and `and(b, a)` as the same strategy rather than two trials.
"""
from __future__ import annotations

import numpy as np

from ._windows import rolling_max, rolling_mean, rolling_min, rolling_std, shift, true_range, wilder_smooth
from .base import Operator, ParamSpec
from .registry import register

__all__ = [
    "And",
    "Breakout",
    "Crossover",
    "MeanReversion",
    "Momentum",
    "Not",
    "Or",
    "Threshold",
    "VolatilityExpansion",
    "VolumeConfirmation",
]


def _nan_mask(*arrays: np.ndarray) -> np.ndarray:
    """True where any input is NaN — the bars whose output must stay NaN."""
    mask = np.zeros(arrays[0].shape[0], dtype=bool)
    for array in arrays:
        mask |= np.isnan(array)
    return mask


def _directional(condition_long: np.ndarray, condition_short: np.ndarray, invalid: np.ndarray) -> np.ndarray:
    out = np.zeros(condition_long.shape[0], dtype=float)
    out[condition_long] = 1.0
    out[condition_short] = -1.0
    out[invalid] = np.nan
    return out


@register
class Crossover(Operator):
    """Two series crossing, with a minimum *percentage* separation.

    The gap is expressed as a fraction of the slower series, never as an
    absolute price distance. An absolute threshold does not transfer between
    instruments or across a decade of price levels, and on a back-adjusted
    series it is outright contaminated — adjustment rewrites historical price
    *levels* while leaving ratios intact (TRD §14.2c).
    """

    name = "crossover"
    category = "signal"
    description = "Directional signal from a fast/slow crossover with a percentage deadband."
    inputs = ("fast", "slow")
    params = (
        ParamSpec(
            "min_gap_pct",
            "float",
            0.0,
            "Deadband as a fraction of the slow series; 0 makes it a bare crossover.",
            minimum=0.0,
            maximum=1.0,
        ),
    )

    def apply(self, inputs, **params):
        fast, slow = inputs["fast"], inputs["slow"]
        with np.errstate(divide="ignore", invalid="ignore"):
            gap = np.where(slow != 0, (fast - slow) / slow, np.nan)
        threshold = params["min_gap_pct"]
        invalid = _nan_mask(fast, slow) | np.isnan(gap)
        return _directional(gap > threshold, gap < -threshold, invalid)


@register
class Threshold(Operator):
    """Directional signal from a series crossing fixed levels.

    `direction` decides which side is bullish: `above` for momentum-style
    reading (high is strong), `below` for oscillator-style reading (low is
    oversold, hence long).
    """

    name = "threshold"
    category = "signal"
    description = "Directional signal from upper/lower level crossings."
    params = (
        ParamSpec("upper", "float", 1.0, "Upper level.", minimum=-1e9, maximum=1e9),
        ParamSpec("lower", "float", -1.0, "Lower level.", minimum=-1e9, maximum=1e9),
        ParamSpec(
            "direction",
            "str",
            "above",
            "'above': long past upper. 'below': long past lower (mean-reverting).",
            choices=("above", "below"),
        ),
    )

    def validate_params(self, params):
        if params["lower"] > params["upper"]:
            raise ValueError(
                f"threshold lower ({params['lower']}) must not exceed upper ({params['upper']})"
            )
        return params

    def apply(self, inputs, **params):
        series = inputs["series"]
        upper, lower = params["upper"], params["lower"]
        invalid = _nan_mask(series)
        if params["direction"] == "above":
            return _directional(series > upper, series < lower, invalid)
        return _directional(series < lower, series > upper, invalid)


@register
class Breakout(Operator):
    """New extreme relative to the **prior** window.

    The window deliberately excludes the current bar. Including it makes the
    comparison `high[t] >= max(high[t-n+1..t])`, which is true on any local
    maximum and trivially true whenever the current bar is the highest — a
    signal that fires constantly and looks impressive in-sample. The prior-window
    form asks the question that actually matters: *is this bar breaking what came
    before it?*
    """

    name = "breakout"
    category = "signal"
    description = "Directional breakout above/below the prior N-bar range."
    inputs = ("high", "low", "close")
    params = (
        ParamSpec("lookback", "int", 20, "Bars in the prior range.", minimum=2, maximum=2000),
    )

    def warmup(self, **params) -> int:
        return params["lookback"]

    def apply(self, inputs, **params):
        lookback = params["lookback"]
        close = inputs["close"]
        prior_high = shift(rolling_max(inputs["high"], lookback), 1)
        prior_low = shift(rolling_min(inputs["low"], lookback), 1)
        invalid = _nan_mask(close, prior_high, prior_low)
        return _directional(close > prior_high, close < prior_low, invalid)


@register
class VolatilityExpansion(Operator):
    """Boolean filter: is volatility above its own recent average?

    A regime filter, not a direction. Breakout logic tends to work when
    volatility is expanding and to bleed on false starts when it is not, so this
    is most useful ANDed with a directional signal.
    """

    name = "vol_expansion"
    category = "signal"
    description = "1 when ATR exceeds `multiple` times its trailing average."
    inputs = ("high", "low", "close")
    params = (
        ParamSpec("period", "int", 14, "ATR period.", minimum=2, maximum=500),
        ParamSpec("lookback", "int", 20, "Averaging window for ATR.", minimum=2, maximum=2000),
        ParamSpec("multiple", "float", 1.0, "Expansion multiple.", minimum=0.1, maximum=10.0),
    )

    def warmup(self, **params) -> int:
        return params["period"] + params["lookback"] - 1

    def apply(self, inputs, **params):
        atr = wilder_smooth(
            true_range(inputs["high"], inputs["low"], inputs["close"]), params["period"]
        )
        baseline = rolling_mean(atr, params["lookback"])
        invalid = _nan_mask(atr, baseline)
        out = (atr > params["multiple"] * baseline).astype(float)
        out[invalid] = np.nan
        return out


@register
class Momentum(Operator):
    """Directional signal from the sign of a return over a lookback."""

    name = "momentum"
    category = "signal"
    description = "Long when the lookback return exceeds a threshold, short below its negative."
    params = (
        ParamSpec("period", "int", 20, "Lookback in bars.", minimum=1, maximum=2000),
        ParamSpec(
            "min_return", "float", 0.0, "Deadband on the lookback return.", minimum=0.0, maximum=10.0
        ),
    )

    def warmup(self, **params) -> int:
        return params["period"]

    def apply(self, inputs, **params):
        series = inputs["series"]
        past = shift(series, params["period"])
        with np.errstate(divide="ignore", invalid="ignore"):
            change = np.where(past != 0, series / past - 1.0, np.nan)
        threshold = params["min_return"]
        invalid = _nan_mask(series, past) | np.isnan(change)
        return _directional(change > threshold, change < -threshold, invalid)


@register
class MeanReversion(Operator):
    """Fade a trailing z-score extreme — the sign is deliberately inverted.

    Stretched *up* is a short. This is the one operator whose sign convention is
    worth stating out loud, because a mean-reversion block that accidentally
    trends is indistinguishable from a bad hypothesis in the results.
    """

    name = "mean_reversion"
    category = "signal"
    description = "Short when the trailing z-score is high, long when it is low."
    params = (
        ParamSpec("window", "int", 20, "Z-score window.", minimum=2, maximum=2000),
        ParamSpec("entry_z", "float", 2.0, "|z| beyond which to fade.", minimum=0.1, maximum=10.0),
    )

    def warmup(self, **params) -> int:
        return params["window"] - 1

    def apply(self, inputs, **params):
        series = inputs["series"]
        window = params["window"]
        mean = rolling_mean(series, window)
        std = rolling_std(series, window)
        with np.errstate(divide="ignore", invalid="ignore"):
            z = np.where(std > 0, (series - mean) / std, 0.0)
        entry = params["entry_z"]
        invalid = _nan_mask(mean, std)
        # Inverted on purpose: stretched up (z high) is a SHORT.
        return _directional(z < -entry, z > entry, invalid)


@register
class VolumeConfirmation(Operator):
    """Boolean filter: is this bar's volume unusually heavy?

    A move on thin volume is the one most likely to be noise, a bad print, or
    unfillable at the size a backtest silently assumes.
    """

    name = "volume_confirmation"
    category = "signal"
    description = "1 when volume exceeds `multiple` times its trailing average."
    inputs = ("volume",)
    params = (
        ParamSpec("window", "int", 20, "Averaging window.", minimum=2, maximum=2000),
        ParamSpec("multiple", "float", 1.5, "Volume multiple.", minimum=0.1, maximum=20.0),
    )

    def warmup(self, **params) -> int:
        return params["window"] - 1

    def apply(self, inputs, **params):
        volume = inputs["volume"]
        baseline = rolling_mean(volume, params["window"])
        invalid = _nan_mask(volume, baseline)
        out = (volume > params["multiple"] * baseline).astype(float)
        out[invalid] = np.nan
        return out


# -- combinators ----------------------------------------------------------------
#
# `commutative = True` on And/Or is load-bearing for duplicate detection: without
# it, `and(a, b)` and `and(b, a)` would hash differently and the same strategy
# would be researched twice, inflating the family trial count with a phantom.


@register
class And(Operator):
    """Both signals agree. Directional inputs must agree in *sign*."""

    name = "and"
    category = "signal"
    description = "Conjunction; directional inputs must share a sign."
    inputs = ("a", "b")
    commutative = True

    def apply(self, inputs, **params):
        a, b = inputs["a"], inputs["b"]
        invalid = _nan_mask(a, b)
        # Treat a boolean 1 as "agrees with whatever the other side says", so a
        # filter ANDed with a direction preserves the direction.
        agree_long = ((a > 0) & (b > 0)).astype(float)
        agree_short = ((a < 0) & (b < 0)).astype(float)
        out = agree_long - agree_short
        out[invalid] = np.nan
        return out


@register
class Or(Operator):
    """Either signal fires; disagreement in direction cancels to flat."""

    name = "or"
    category = "signal"
    description = "Disjunction; opposing directions cancel."
    inputs = ("a", "b")
    commutative = True

    def apply(self, inputs, **params):
        a, b = inputs["a"], inputs["b"]
        invalid = _nan_mask(a, b)
        combined = np.sign(a) + np.sign(b)
        out = np.clip(combined, -1.0, 1.0)
        out[invalid] = np.nan
        return out


@register
class Not(Operator):
    """Logical negation of a boolean filter."""

    name = "not"
    category = "signal"
    description = "1 where the input is 0, 0 where it is non-zero."
    inputs = ("a",)

    def apply(self, inputs, **params):
        a = inputs["a"]
        out = (a == 0).astype(float)
        out[np.isnan(a)] = np.nan
        return out
