"""Risk operators — stops, sizing, and volatility targeting (TRD §11).

These take a *position* series and return a modified one, so they compose onto
the end of an entry/exit chain: `risk_logic` in a spec is a chain of these
wrapping whatever `entry_logic` produced.

**Path dependence is the point, and the reason these are loops.** A trailing
stop cannot be expressed as a windowed reduction: whether bar `t` is stopped out
depends on whether bar `t-1` was still in the trade, which depends on `t-2`, all
the way back to the entry. Vectorising that is exactly where look-ahead gets
introduced (TRD §9.4) — the "clever" formulations reach for `cummax` over the
whole series or a `shift(-1)` to find the exit bar, and both leak.

So they are written as explicit forward loops: one bar at a time, state carried
forward, nothing after `t` ever read. TRD §19 earmarks Numba for exactly this
shape, but §9.6 says profile before optimising and §9.7 puts correctness first —
so these stay plain NumPy until Stage 3 has a benchmark to justify a JIT.
"""
from __future__ import annotations

import numpy as np

from ._windows import rolling_mean, rolling_std, true_range, wilder_smooth
from .base import Operator, ParamSpec
from .registry import register

__all__ = [
    "ATRStop",
    "KellyFraction",
    "TimeStop",
    "TrailingStop",
    "VolatilityTarget",
]


def _entries(position: np.ndarray) -> np.ndarray:
    """Bars where a new position opens or the direction flips."""
    previous = np.concatenate(([0.0], position[:-1]))
    return (np.sign(position) != np.sign(previous)) & (position != 0)


def _blank_warmup(out: np.ndarray, atr: np.ndarray) -> np.ndarray:
    """NaN out the bars where the stop could not have been computed.

    Passing the position straight through while the ATR is still warming up
    would hold a position through a window where the stop **cannot fire** — a
    silent gap in protection, at exactly the start of the series where a
    walk-forward fold begins. NaN becomes flat when the spec is compiled, which
    is the honest reading: do not take a position you cannot yet protect.
    """
    out[np.isnan(atr)] = np.nan
    return out


@register
class ATRStop(Operator):
    """Flatten when price moves `multiple` ATRs against the entry price.

    The stop distance is set **at entry** and held for the life of the trade.
    Recomputing it from the current bar's ATR each bar would let a volatility
    spike move the stop *away* from price mid-trade — a stop that widens when
    things go wrong is not a stop.
    """

    name = "atr_stop"
    category = "risk"
    description = "Fixed ATR-distance stop measured from the entry price."
    inputs = ("position", "close", "high", "low")
    implicit_inputs = frozenset({"position"})
    params = (
        ParamSpec("period", "int", 14, "ATR period.", minimum=2, maximum=500),
        ParamSpec("multiple", "float", 2.0, "Stop distance in ATRs.", minimum=0.1, maximum=20.0),
    )

    def warmup(self, **params) -> int:
        return params["period"]

    def apply(self, inputs, **params):
        position, close = inputs["position"], inputs["close"]
        atr = wilder_smooth(
            true_range(inputs["high"], inputs["low"], inputs["close"]), params["period"]
        )
        multiple = params["multiple"]
        n = position.shape[0]
        out = np.array(position, dtype=float, copy=True)

        entries = _entries(np.nan_to_num(position))
        stop_level = np.nan
        direction = 0.0

        for index in range(n):
            current = position[index]
            if np.isnan(current) or current == 0.0:
                direction, stop_level = 0.0, np.nan
                continue

            # `not isfinite(stop_level)` is the third condition and it is
            # load-bearing: a trade opening while the ATR is still warming up
            # gets no stop level, and without this it would never get one for
            # the life of the trade — a stop that silently does not exist. The
            # position is NaN (flat) during that warm-up anyway, so the first
            # bar with a usable ATR is the effective entry.
            if entries[index] or direction == 0.0 or not np.isfinite(stop_level):
                direction = float(np.sign(current))
                distance = multiple * atr[index]
                stop_level = (
                    close[index] - direction * distance if np.isfinite(distance) else np.nan
                )
                continue

            if np.isfinite(stop_level) and (
                (direction > 0 and close[index] <= stop_level)
                or (direction < 0 and close[index] >= stop_level)
            ):
                out[index] = 0.0
                direction, stop_level = 0.0, np.nan
        return _blank_warmup(out, atr)


@register
class TrailingStop(Operator):
    """Stop that ratchets toward price and never away from it.

    The high-water mark is tracked **within the open trade only**, one bar at a
    time. A whole-series `cummax` would let the stop know about highs the trade
    had not reached yet, which is the classic vectorised trailing-stop leak.
    """

    name = "trailing_stop"
    category = "risk"
    description = "ATR-distance trailing stop that only ever tightens."
    inputs = ("position", "close", "high", "low")
    implicit_inputs = frozenset({"position"})
    params = (
        ParamSpec("period", "int", 14, "ATR period.", minimum=2, maximum=500),
        ParamSpec("multiple", "float", 3.0, "Trail distance in ATRs.", minimum=0.1, maximum=20.0),
    )

    def warmup(self, **params) -> int:
        return params["period"]

    def apply(self, inputs, **params):
        position, close = inputs["position"], inputs["close"]
        atr = wilder_smooth(
            true_range(inputs["high"], inputs["low"], inputs["close"]), params["period"]
        )
        multiple = params["multiple"]
        out = np.array(position, dtype=float, copy=True)

        direction = 0.0
        extreme = np.nan  # best price seen since entry, in the trade's favour
        stop_level = np.nan

        for index in range(position.shape[0]):
            current = position[index]
            if np.isnan(current) or current == 0.0:
                direction, extreme, stop_level = 0.0, np.nan, np.nan
                continue

            price = close[index]
            if direction == 0.0 or np.sign(current) != direction:
                direction = float(np.sign(current))
                extreme = price
                distance = multiple * atr[index]
                stop_level = price - direction * distance if np.isfinite(distance) else np.nan
                continue

            extreme = max(extreme, price) if direction > 0 else min(extreme, price)
            distance = multiple * atr[index]
            if np.isfinite(distance):
                candidate = extreme - direction * distance
                # Ratchet: tighten only. `max` for longs, `min` for shorts.
                if not np.isfinite(stop_level):
                    stop_level = candidate
                else:
                    stop_level = (
                        max(stop_level, candidate) if direction > 0 else min(stop_level, candidate)
                    )

            if np.isfinite(stop_level) and (
                (direction > 0 and price <= stop_level) or (direction < 0 and price >= stop_level)
            ):
                out[index] = 0.0
                direction, extreme, stop_level = 0.0, np.nan, np.nan
        return _blank_warmup(out, atr)


@register
class TimeStop(Operator):
    """Flatten after `max_bars` in the same position.

    Cheap, and it does something the price-based stops cannot: it bounds how long
    capital sits in an idea that is neither working nor failing, which is what
    turns a modest edge into a poor Sharpe through sheer exposure.
    """

    name = "time_stop"
    category = "risk"
    description = "Flatten a position held longer than `max_bars`."
    inputs = ("position",)
    implicit_inputs = frozenset({"position"})
    params = (
        ParamSpec("max_bars", "int", 20, "Maximum holding period in bars.", minimum=1, maximum=10000),
    )

    def apply(self, inputs, **params):
        position = inputs["position"]
        limit = params["max_bars"]
        out = np.array(position, dtype=float, copy=True)

        direction, held = 0.0, 0
        for index in range(position.shape[0]):
            current = position[index]
            if np.isnan(current) or current == 0.0:
                direction, held = 0.0, 0
                continue
            if np.sign(current) != direction:
                direction, held = float(np.sign(current)), 1
                continue
            held += 1
            if held > limit:
                out[index] = 0.0
                direction, held = 0.0, 0
        return out


@register
class VolatilityTarget(Operator):
    """Scale a position so its ex-ante volatility tracks a target.

    Sizing on **trailing realised volatility** is what makes a strategy's risk
    comparable across regimes: unscaled, the same signal risks three times as
    much in 2008 as in 2017, and the resulting Sharpe mostly measures which
    regime the sample happened to contain.

    `max_leverage` is not optional. As volatility approaches zero the naive
    ratio approaches infinity, and a quiet fortnight would otherwise produce a
    position no venue would fill and no risk desk would sign.
    """

    name = "vol_target"
    category = "risk"
    description = "Scale a position toward a target annualised volatility."
    inputs = ("position", "returns")
    implicit_inputs = frozenset({"position"})
    params = (
        ParamSpec("window", "int", 60, "Realised-volatility window.", minimum=5, maximum=2000),
        ParamSpec(
            "target_vol", "float", 0.15, "Target volatility, per period.", minimum=1e-4, maximum=10.0
        ),
        ParamSpec("max_leverage", "float", 3.0, "Hard cap on the scale factor.", minimum=0.1, maximum=20.0),
    )

    def warmup(self, **params) -> int:
        return params["window"] - 1

    def apply(self, inputs, **params):
        position = inputs["position"]
        realised = rolling_std(inputs["returns"], params["window"])
        with np.errstate(divide="ignore", invalid="ignore"):
            scale = np.where(realised > 0, params["target_vol"] / realised, np.nan)
        scale = np.clip(scale, 0.0, params["max_leverage"])
        out = position * scale
        out[np.isnan(realised)] = np.nan
        return out


@register
class KellyFraction(Operator):
    """Capped fractional-Kelly sizing from trailing return moments.

    Full Kelly (`mu / sigma^2`) maximises long-run log growth **only if the
    estimated moments are correct**, and they never are: estimated from a finite
    trailing window they are noisy, and Kelly is famously brutal about
    overestimated edge. So `fraction` defaults to a quarter and `cap` bounds the
    result — half-Kelly-or-less is the standard practitioner adjustment for
    exactly this estimation error.
    """

    name = "kelly"
    category = "risk"
    description = "Capped fractional Kelly sizing from a trailing mean/variance estimate."
    inputs = ("position", "returns")
    implicit_inputs = frozenset({"position"})
    references = "Kelly (1956); MacLean, Thorp & Ziemba on fractional Kelly under estimation error."
    params = (
        ParamSpec("window", "int", 120, "Estimation window.", minimum=10, maximum=5000),
        ParamSpec("fraction", "float", 0.25, "Fraction of full Kelly.", minimum=0.01, maximum=1.0),
        ParamSpec("cap", "float", 1.0, "Hard cap on the resulting size.", minimum=0.01, maximum=10.0),
    )

    def warmup(self, **params) -> int:
        return params["window"] - 1

    def apply(self, inputs, **params):
        returns = inputs["returns"]
        window = params["window"]
        mean = rolling_mean(returns, window)
        std = rolling_std(returns, window)
        with np.errstate(divide="ignore", invalid="ignore"):
            full_kelly = np.where(std > 0, mean / (std * std), np.nan)
        size = np.clip(params["fraction"] * full_kelly, -params["cap"], params["cap"])
        out = inputs["position"] * np.abs(size)
        out[np.isnan(mean) | np.isnan(std)] = np.nan
        return out
