"""Transformation operators — series in, series out (TRD §11).

Rolling mean, EMA, JMA, Kalman, ATR normalisation, fractional differencing,
rolling PCA, causal wavelets.

### The two that would have leaked

**PCA.** `sklearn.decomposition.PCA` fits on the whole sample. Using it here
would mean every bar's factor loadings were computed with knowledge of the
entire future — TRD §9.4's *"vectorisation is the top source of look-ahead"* in
its purest form, and completely invisible in the output. `RollingPCA` therefore
refits an SVD over the trailing window only, emitting the projection of the
current bar. Slower, and correct.

**Wavelets.** A standard DWT is not causal: `pywt.dwt` over a full series lets
coefficients at time `t` depend on data after `t`, and the boundary handling
smears the future backwards across the whole reconstruction. `CausalWavelet`
instead transforms the trailing window at each bar and emits only the newest
coefficient. That also keeps the dependency list to zero additions — the
Haar/à-trous smoother it needs is a few lines of NumPy, so `PyWavelets` was not
required after all.

Both then pass the registry-wide truncation-invariance test by construction,
which is how we know rather than hope.
"""
from __future__ import annotations

import numpy as np

from ._windows import (
    rolling_apply,
    rolling_mean,
    rolling_std,
    shift,
    true_range,
    wilder_smooth,
)
from .base import Operator, ParamSpec
from .registry import register

__all__ = [
    "ATRNormalise",
    "AverageTrueRange",
    "CausalWavelet",
    "ExponentialMovingAverage",
    "FractionalDifference",
    "JurikMovingAverage",
    "KalmanFilter",
    "LogReturn",
    "PercentRank",
    "RateOfChange",
    "RollingMean",
    "RollingPCA",
    "RollingStd",
    "RollingZScore",
]


def _window_param(default: int = 20, minimum: int = 2, maximum: int = 2000) -> ParamSpec:
    return ParamSpec(
        "window", "int", default, "Trailing lookback in bars.", minimum=minimum, maximum=maximum
    )


@register
class RollingMean(Operator):
    name = "rolling_mean"
    category = "transformation"
    description = "Trailing simple moving average."
    params = (_window_param(),)

    def warmup(self, **params) -> int:
        return params["window"] - 1

    def apply(self, inputs, **params):
        return rolling_mean(inputs["series"], params["window"])


@register
class RollingStd(Operator):
    name = "rolling_std"
    category = "transformation"
    description = "Trailing sample standard deviation."
    params = (_window_param(),)

    def warmup(self, **params) -> int:
        return params["window"] - 1

    def apply(self, inputs, **params):
        return rolling_std(inputs["series"], params["window"])


@register
class ExponentialMovingAverage(Operator):
    """EMA with `alpha = 2/(span+1)`, seeded on the first `span` bars.

    Seeded with a simple mean rather than the first observation: seeding on
    `x[0]` makes the early output depend heavily on one arbitrary bar, and that
    dependence decays slowly enough to still be visible hundreds of bars later.
    """

    name = "ema"
    category = "transformation"
    description = "Exponential moving average, span-parameterised."
    params = (
        ParamSpec("span", "int", 20, "EMA span; alpha = 2/(span+1).", minimum=2, maximum=2000),
    )

    def warmup(self, **params) -> int:
        return params["span"] - 1

    def apply(self, inputs, **params):
        series = inputs["series"]
        span = params["span"]
        out = np.full(series.shape[0], np.nan, dtype=float)
        if series.shape[0] < span:
            return out

        alpha = 2.0 / (span + 1.0)
        average = float(np.mean(series[:span]))
        out[span - 1] = average
        for index in range(span, series.shape[0]):
            average = alpha * series[index] + (1.0 - alpha) * average
            out[index] = average
        return out


@register
class JurikMovingAverage(Operator):
    """Jurik-style adaptive moving average — low lag, heavy smoothing.

    A three-stage causal filter: an adaptive EMA whose responsiveness is set by
    `phase` and `power`, then two smoothing passes that remove the overshoot the
    first stage introduces. The commercial JMA is proprietary and unpublished;
    this is the standard open reconstruction, and it is a *reference
    implementation* — isolated in this one class precisely so a better-validated
    port can replace it without touching anything else in the library.
    """

    name = "jma"
    category = "transformation"
    description = "Jurik-style adaptive moving average (open reconstruction)."
    references = "Reconstruction of Jurik Research's JMA; proprietary original unpublished."
    params = (
        ParamSpec("length", "int", 14, "Smoothing length.", minimum=2, maximum=500),
        ParamSpec("phase", "float", 0.0, "Lag/overshoot trade-off, -100..100.", minimum=-100.0, maximum=100.0),
        ParamSpec("power", "float", 2.0, "Adaptive exponent; higher is smoother.", minimum=0.5, maximum=4.0),
    )

    def warmup(self, **params) -> int:
        return params["length"] - 1

    def apply(self, inputs, **params):
        series = inputs["series"]
        length, phase, power = params["length"], params["phase"], params["power"]
        n = series.shape[0]
        out = np.full(n, np.nan, dtype=float)
        if n < length:
            return out

        phase_ratio = np.clip(phase / 100.0 + 1.5, 0.5, 2.5)
        beta = 0.45 * (length - 1) / (0.45 * (length - 1) + 2.0)
        alpha = beta**power

        # Seed on the trailing mean so the filter does not inherit one bar's noise.
        seed = float(np.mean(series[:length]))
        ma1 = det0 = seed
        det1 = 0.0
        jma = seed
        out[length - 1] = jma

        for index in range(length, n):
            price = float(series[index])
            ma1 = (1.0 - alpha) * price + alpha * ma1
            det0 = (price - ma1) * (1.0 - beta) + beta * det0
            ma2 = ma1 + phase_ratio * det0
            det1 = (ma2 - jma) * (1.0 - alpha) ** 2 + alpha**2 * det1
            jma = jma + det1
            out[index] = jma
        return out


@register
class KalmanFilter(Operator):
    """Scalar local-level Kalman filter — a random walk observed with noise.

    One-dimensional on purpose. A full state-space model invites the agent to
    fit process and observation covariances to the data, which is parameter
    search wearing a lab coat: the extra freedom buys in-sample fit and nothing
    else. `process_var` and `observation_var` are declared parameters tuned per
    fold on training data only, like any other.

    The filter runs forward one bar at a time, so it is causal by construction —
    no smoother pass, which is what would make it look-ahead.
    """

    name = "kalman"
    category = "transformation"
    description = "Scalar local-level Kalman filter (filtered, never smoothed)."
    params = (
        ParamSpec("process_var", "float", 1e-4, "State (process) variance Q.", minimum=1e-12, maximum=10.0),
        ParamSpec("observation_var", "float", 1e-2, "Observation variance R.", minimum=1e-12, maximum=100.0),
    )

    def apply(self, inputs, **params):
        series = inputs["series"]
        q, r = params["process_var"], params["observation_var"]
        n = series.shape[0]
        out = np.full(n, np.nan, dtype=float)
        if n == 0:
            return out

        state = float(series[0])
        covariance = 1.0
        out[0] = state
        for index in range(1, n):
            # Predict (random walk: the state estimate is unchanged), then update.
            covariance += q
            gain = covariance / (covariance + r)
            state += gain * (float(series[index]) - state)
            covariance *= 1.0 - gain
            out[index] = state
        return out


@register
class AverageTrueRange(Operator):
    name = "atr"
    category = "transformation"
    description = "Average true range, Wilder-smoothed."
    inputs = ("high", "low", "close")
    params = (ParamSpec("period", "int", 14, "Wilder smoothing period.", minimum=2, maximum=500),)

    def warmup(self, **params) -> int:
        return params["period"]

    def apply(self, inputs, **params):
        ranges = true_range(inputs["high"], inputs["low"], inputs["close"])
        return wilder_smooth(ranges, params["period"])


@register
class ATRNormalise(Operator):
    """Divide a series by its ATR — the unit that makes instruments comparable.

    A 20-point move means nothing on its own: it is enormous for one instrument
    and noise for another, and it means different things for the *same*
    instrument in 2008 and 2017. Volatility-normalised distance is what
    transfers across both, which is why parameters expressed in ATRs survive
    walk-forward far better than parameters expressed in points or rupees.
    """

    name = "atr_normalise"
    category = "transformation"
    description = "Series expressed in units of ATR."
    inputs = ("series", "high", "low", "close")
    params = (ParamSpec("period", "int", 14, "ATR period.", minimum=2, maximum=500),)

    def warmup(self, **params) -> int:
        return params["period"]

    def apply(self, inputs, **params):
        atr = wilder_smooth(
            true_range(inputs["high"], inputs["low"], inputs["close"]), params["period"]
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(atr > 0, inputs["series"] / atr, np.nan)


@register
class RollingZScore(Operator):
    """Trailing z-score. The **rolling** window is the whole point.

    A whole-sample z-score is the textbook leak: normalising by statistics
    computed over the full series tells every early bar what the later mean and
    variance turned out to be.
    """

    name = "zscore"
    category = "transformation"
    description = "Trailing z-score over a rolling window."
    params = (_window_param(),)

    def warmup(self, **params) -> int:
        return params["window"] - 1

    def apply(self, inputs, **params):
        series = inputs["series"]
        window = params["window"]
        mean = rolling_mean(series, window)
        std = rolling_std(series, window)
        with np.errstate(divide="ignore", invalid="ignore"):
            scores = np.where(std > 0, (series - mean) / std, 0.0)
        # A flat window really is a z-score of 0, but the NaN warm-up must stay
        # NaN: `std > 0` is False for both, and collapsing them would report
        # "no deviation" for bars where nothing has been measured yet.
        return np.where(np.isnan(std), np.nan, scores)


@register
class PercentRank(Operator):
    """Where the current value sits within its trailing window, in [0, 1]."""

    name = "percent_rank"
    category = "transformation"
    description = "Percentile rank of the current bar within its trailing window."
    params = (_window_param(),)

    def warmup(self, **params) -> int:
        return params["window"] - 1

    def apply(self, inputs, **params):
        def rank(window: np.ndarray) -> float:
            return float(np.mean(window <= window[-1]))

        return rolling_apply(inputs["series"], params["window"], rank)


@register
class RateOfChange(Operator):
    name = "roc"
    category = "transformation"
    description = "Fractional change over a lookback."
    params = (ParamSpec("period", "int", 10, "Lookback in bars.", minimum=1, maximum=2000),)

    def warmup(self, **params) -> int:
        return params["period"]

    def apply(self, inputs, **params):
        series = inputs["series"]
        past = shift(series, params["period"])
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(past != 0, series / past - 1.0, np.nan)


@register
class LogReturn(Operator):
    name = "log_return"
    category = "transformation"
    description = "Log return over a lookback."
    params = (ParamSpec("period", "int", 1, "Lookback in bars.", minimum=1, maximum=2000),)

    def warmup(self, **params) -> int:
        return params["period"]

    def apply(self, inputs, **params):
        series = inputs["series"]
        past = shift(series, params["period"])
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where((past > 0) & (series > 0), series / past, np.nan)
        return np.log(ratio)


@register
class FractionalDifference(Operator):
    """Fixed-width fractional differencing (López de Prado, *AFML* ch. 5).

    The trade-off it resolves: raw prices are non-stationary, and first
    differences are stationary but throw away nearly all the memory that made
    the series predictable. Fractional `d` in (0, 1) buys stationarity while
    keeping as much memory as possible.

    Weights are the binomial expansion of `(1-B)^d`, truncated where they fall
    below `threshold`. The convolution is strictly backwards-looking.
    """

    name = "frac_diff"
    category = "transformation"
    description = "Fixed-width fractional differencing."
    references = "López de Prado, Advances in Financial Machine Learning, ch. 5."
    params = (
        ParamSpec("d", "float", 0.4, "Differencing order in [0, 1].", minimum=0.0, maximum=1.0),
        ParamSpec(
            "threshold", "float", 1e-4, "Drop weights below this.", minimum=1e-8, maximum=0.1
        ),
        ParamSpec("max_width", "int", 200, "Weight-window cap.", minimum=2, maximum=5000),
    )

    @staticmethod
    def weights(d: float, threshold: float, max_width: int) -> np.ndarray:
        """Binomial weights `w[k] = -w[k-1] * (d-k+1)/k`, newest first."""
        computed = [1.0]
        for k in range(1, max_width):
            next_weight = -computed[-1] * (d - k + 1.0) / k
            if abs(next_weight) < threshold:
                break
            computed.append(next_weight)
        return np.array(computed, dtype=float)

    def warmup(self, **params) -> int:
        return len(self.weights(params["d"], params["threshold"], params["max_width"])) - 1

    def apply(self, inputs, **params):
        series = inputs["series"]
        weights = self.weights(params["d"], params["threshold"], params["max_width"])
        width = weights.shape[0]
        n = series.shape[0]
        out = np.full(n, np.nan, dtype=float)
        if n < width:
            return out
        # weights[0] multiplies the CURRENT bar, weights[k] the bar k back.
        for index in range(width - 1, n):
            window = series[index - width + 1 : index + 1][::-1]
            out[index] = float(np.dot(weights, window))
        return out


@register
class RollingPCA(Operator):
    """First principal component of a trailing window, projected onto the current bar.

    Refits the SVD on every bar over the trailing window only. `sklearn`'s PCA
    fits the whole sample, which would hand every historical bar the loadings
    derived from its own future — the leak this class exists to avoid.

    Sign is pinned by requiring the loading vector's largest-magnitude component
    to be positive. An SVD's sign is arbitrary, and an arbitrary sign that flips
    between refits would make the output flip too, destroying determinism for no
    reason.
    """

    name = "rolling_pca"
    category = "transformation"
    description = "First principal component over a trailing window."
    inputs = ("series",)
    params = (
        _window_param(default=60, minimum=10),
        ParamSpec("lags", "int", 3, "Lagged copies forming the embedding.", minimum=2, maximum=50),
    )

    def warmup(self, **params) -> int:
        return params["window"] + params["lags"] - 2

    def apply(self, inputs, **params):
        series = inputs["series"]
        window, lags = params["window"], params["lags"]
        n = series.shape[0]
        out = np.full(n, np.nan, dtype=float)
        if n < window + lags - 1:
            return out

        # Delay embedding: column j is the series lagged by j bars.
        embedded = np.column_stack([shift(series, lag) for lag in range(lags)])

        for index in range(window + lags - 2, n):
            block = embedded[index - window + 1 : index + 1]
            if not np.isfinite(block).all():
                continue
            centred = block - block.mean(axis=0)
            _, _, components = np.linalg.svd(centred, full_matrices=False)
            loading = components[0]
            dominant = int(np.argmax(np.abs(loading)))
            if loading[dominant] < 0:
                loading = -loading
            out[index] = float(centred[-1] @ loading)
        return out


@register
class CausalWavelet(Operator):
    """Trailing-window à trous wavelet detail — the causal alternative to a DWT.

    At each bar the trailing window is smoothed `level` times with a dyadic
    Haar-style low-pass, and the operator emits the newest bar's detail
    coefficient (the difference between successive smoothing levels). Only the
    current window is ever touched, so nothing after bar `t` reaches `out[t]` —
    unlike a whole-series DWT, whose boundary handling smears the future
    backwards.
    """

    name = "wavelet_detail"
    category = "transformation"
    description = "Causal à trous wavelet detail coefficient at a chosen level."
    params = (
        _window_param(default=64, minimum=8),
        ParamSpec("level", "int", 2, "Decomposition level.", minimum=1, maximum=6),
    )

    def warmup(self, **params) -> int:
        return params["window"] - 1

    def apply(self, inputs, **params):
        window, level = params["window"], params["level"]
        if 2**level > window:
            # A scale wider than the window cannot be resolved from it.
            return np.full(inputs["series"].shape[0], np.nan, dtype=float)

        def detail(block: np.ndarray) -> float:
            coarse = block.astype(float)
            previous = coarse
            for step in range(level):
                spacing = 2**step
                smoothed = coarse.copy()
                # Trailing two-tap à trous filter: only past samples contribute.
                smoothed[spacing:] = 0.5 * (coarse[spacing:] + coarse[:-spacing])
                previous, coarse = coarse, smoothed
            return float(previous[-1] - coarse[-1])

        return rolling_apply(inputs["series"], window, detail)
