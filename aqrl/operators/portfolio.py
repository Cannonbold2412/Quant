"""Portfolio operators — cross-sectional weighting (TRD §11).

These are the one family whose arrays are **2-D**: `(n_bars, n_instruments)` in,
the same shape of weights out. The `Operator` contract still holds, because it
constrains `shape[0]` — bars — and says nothing about what varies across the
second axis.

Every weight at bar `t` is computed from a **trailing window ending at `t`**.
That is the whole difficulty of portfolio construction done honestly: the
textbook formulations estimate a covariance matrix over the full sample, which
hands every historical rebalance the correlations that only became apparent
later. A risk-parity backtest built that way looks superb and is meaningless.

> **Scope note.** Multi-strategy portfolio construction and correlation-aware
> allocation are explicitly deferred from v1 (PRD §3, Implementation_Plan §20).
> These operators weight *instruments inside one strategy* — a cross-sectional
> equity strategy holding twenty names — which is a different problem and is in
> scope.
"""
from __future__ import annotations

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

from .base import Operator, ParamSpec
from .registry import register

__all__ = ["CorrelationCluster", "EqualWeight", "RiskParity"]


def _as_matrix(values: np.ndarray) -> np.ndarray:
    """Accept a 1-D series as a single-instrument portfolio."""
    return values.reshape(-1, 1) if values.ndim == 1 else values


def _normalise_rows(weights: np.ndarray) -> np.ndarray:
    """Scale each bar's weights to sum to 1, leaving all-zero bars at zero."""
    totals = np.nansum(np.abs(weights), axis=1, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(totals > 0, weights / totals, 0.0)


@register
class EqualWeight(Operator):
    """1/N across the instruments that have data on each bar.

    The benchmark every other scheme must beat, and it is a genuinely hard one:
    equal weight is estimation-error-free, because it estimates nothing. A risk
    model that loses to 1/N out of sample has bought noise.
    """

    name = "equal_weight"
    category = "portfolio"
    description = "Equal weight across instruments with data on each bar."
    inputs = ("returns",)

    def apply(self, inputs, **params):
        returns = _as_matrix(inputs["returns"])
        available = np.isfinite(returns)
        counts = available.sum(axis=1, keepdims=True)
        with np.errstate(divide="ignore", invalid="ignore"):
            weights = np.where(available & (counts > 0), 1.0 / counts, 0.0)
        weights[counts[:, 0] == 0, :] = np.nan
        return weights


@register
class RiskParity(Operator):
    """Inverse-volatility weights from a trailing window.

    The diagonal form — weight ∝ 1/σ, ignoring correlations. That is a
    deliberate simplification rather than an oversight: full risk parity needs
    an inverted covariance matrix, and inverting a sample covariance estimated
    from a short window on many instruments amplifies estimation noise
    dramatically. The diagonal version captures most of the benefit and cannot
    blow up on a near-singular matrix.
    """

    name = "risk_parity"
    category = "portfolio"
    description = "Inverse-volatility (diagonal risk parity) weights over a trailing window."
    inputs = ("returns",)
    params = (
        ParamSpec("window", "int", 60, "Volatility estimation window.", minimum=5, maximum=2000),
    )

    def warmup(self, **params) -> int:
        return params["window"] - 1

    def apply(self, inputs, **params):
        returns = _as_matrix(inputs["returns"])
        window = params["window"]
        n_bars, n_assets = returns.shape
        weights = np.full((n_bars, n_assets), np.nan, dtype=float)

        for index in range(window - 1, n_bars):
            block = returns[index - window + 1 : index + 1]
            with np.errstate(invalid="ignore"):
                sigma = np.nanstd(block, axis=0, ddof=1)
            usable = np.isfinite(sigma) & (sigma > 0)
            row = np.zeros(n_assets, dtype=float)
            if usable.any():
                row[usable] = 1.0 / sigma[usable]
                row /= row.sum()
            weights[index] = row
        return weights


@register
class CorrelationCluster(Operator):
    """Equal weight *between* correlation clusters, equal *within* each.

    Naive 1/N over twenty names that are really three bets massively
    overweights whichever theme happens to have the most tickers. Clustering on
    the trailing correlation matrix and splitting the budget across clusters
    first is the cheap fix — it is the diversification step of Hierarchical Risk
    Parity without the covariance inversion.

    The linkage is recomputed on each bar's trailing window, so no bar is ever
    clustered using correlations that had not yet appeared.
    """

    name = "correlation_cluster"
    category = "portfolio"
    description = "Equal weight across trailing-correlation clusters, equal within them."
    inputs = ("returns",)
    references = "The diversification step of López de Prado's Hierarchical Risk Parity."
    params = (
        ParamSpec("window", "int", 120, "Correlation window.", minimum=10, maximum=5000),
        ParamSpec("n_clusters", "int", 3, "Target cluster count.", minimum=1, maximum=50),
        ParamSpec(
            "rebalance_every", "int", 20, "Bars between re-clustering.", minimum=1, maximum=500
        ),
    )

    def warmup(self, **params) -> int:
        return params["window"] - 1

    def apply(self, inputs, **params):
        returns = _as_matrix(inputs["returns"])
        window, target = params["window"], params["n_clusters"]
        every = params["rebalance_every"]
        n_bars, n_assets = returns.shape
        weights = np.full((n_bars, n_assets), np.nan, dtype=float)

        labels = np.zeros(n_assets, dtype=int)
        for index in range(window - 1, n_bars):
            # Re-cluster periodically. Doing it every bar is both expensive and
            # unstable — cluster membership flickers on noise, and each flicker
            # is turnover the strategy pays for.
            if (index - (window - 1)) % every == 0:
                labels = self._cluster(returns[index - window + 1 : index + 1], target)
            weights[index] = self._weights_from(labels, n_assets)
        return weights

    @staticmethod
    def _cluster(block: np.ndarray, target: int) -> np.ndarray:
        n_assets = block.shape[1]
        if n_assets < 2:
            return np.zeros(n_assets, dtype=int)

        with np.errstate(invalid="ignore"):
            correlation = np.corrcoef(np.nan_to_num(block, nan=0.0), rowvar=False)
        correlation = np.nan_to_num(correlation, nan=0.0)
        np.fill_diagonal(correlation, 1.0)

        # The standard correlation distance. Clipped because floating point can
        # push a correlation a hair outside [-1, 1] and sqrt of a negative is NaN.
        distance = np.sqrt(np.clip(0.5 * (1.0 - correlation), 0.0, 1.0))
        np.fill_diagonal(distance, 0.0)
        distance = 0.5 * (distance + distance.T)  # enforce exact symmetry

        linkage_matrix = linkage(squareform(distance, checks=False), method="average")
        return fcluster(linkage_matrix, t=min(target, n_assets), criterion="maxclust")

    @staticmethod
    def _weights_from(labels: np.ndarray, n_assets: int) -> np.ndarray:
        row = np.zeros(n_assets, dtype=float)
        unique = np.unique(labels)
        for label in unique:
            members = labels == label
            row[members] = 1.0 / (len(unique) * members.sum())
        return row
