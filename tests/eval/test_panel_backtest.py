"""3a — the panel and the backtest core, against hand-computed arithmetic.

Every number in this module is derivable on paper. That is deliberate: the
engine is the thing that decides what is true about a strategy, so its own
verdicts have to be checked against something that does not come from it.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from aqrl.eval.backtest import lag_and_cap, max_drawdown_from_returns, run_backtest
from aqrl.eval.costs import traded_quantities, traded_split, transaction_costs
from aqrl.eval.fills import FillError, fill_prices
from aqrl.eval.panel import PanelError, PricePanel
from aqrl.eval.tradebook import extract_trades, write_equity_curve, write_tradebook

from .conftest import bars, with_fill_model


# -- the panel ------------------------------------------------------------------


def test_single_instrument_frame_becomes_a_panel_of_one():
    panel = PricePanel.from_frame(bars([100.0, 101.0, 102.0]).drop("instrument"))
    assert panel.n_instruments == 1
    assert panel.n_bars == 3
    assert panel.column("close").shape == (3, 1)


def test_missing_bars_are_nan_never_forward_filled():
    frame = bars({"AAA": [10.0, 11.0, 12.0], "BBB": [20.0, 21.0, 22.0]})
    frame = frame.filter(~((pl.col("instrument") == "BBB") & (pl.col("date") == dt.date(2020, 1, 2))))

    panel = PricePanel.from_frame(frame)
    close = panel.column("close")

    assert panel.instruments == ("AAA", "BBB")
    assert np.isnan(close[1, 1])
    assert not panel.listed()[1, 1]


def test_duplicate_bars_are_refused():
    frame = pl.concat([bars([100.0, 101.0]), bars([100.0, 101.0])])
    with pytest.raises(PanelError, match="more than one row"):
        PricePanel.from_frame(frame)


def test_slice_dates_is_inclusive_at_both_ends():
    panel = PricePanel.from_frame(bars([1.0, 2.0, 3.0, 4.0, 5.0]))
    window = panel.slice_dates(panel.dates[1], panel.dates[3])
    assert window.n_bars == 3
    assert window.column("close")[0, 0] == 2.0
    assert window.column("close")[-1, 0] == 4.0


# -- position transitions -------------------------------------------------------


def test_traded_split_handles_a_reversal_as_two_transactions():
    weights = np.array([[0.0], [1.0], [-1.0], [0.0]])
    held, opened, closed = traded_split(weights)

    assert opened[1, 0] == 1.0 and closed[1, 0] == 0.0  # open the long
    assert opened[2, 0] == -1.0 and closed[2, 0] == 1.0  # flip: close AND open
    assert held[2, 0] == 0.0
    assert closed[3, 0] == -1.0 and opened[3, 0] == 0.0  # close the short


def test_scaling_within_a_side_trades_only_the_difference():
    weights = np.array([[0.5], [0.8], [0.3]])
    held, opened, closed = traded_split(weights)

    assert opened[1, 0] == pytest.approx(0.3)
    assert held[1, 0] == pytest.approx(0.5)
    assert closed[2, 0] == pytest.approx(0.5)
    assert held[2, 0] == pytest.approx(0.3)


def test_the_two_sides_are_charged_from_their_own_schedules(resolved):
    """A round trip is `entry_bps + exit_bps`, never 2× either one.

    On the shipped NSE profile the two sides happen to come out equal — stamp
    duty is 1.5 bps on the buy and the flat DP charge works out to 1.5 bps on
    the sell — so this checks the *sides* rather than the totals. Halving a
    round-trip number would pass on this profile and misprice every other one.
    """
    cost_model = resolved.cost_model
    assert cost_model.stamp_duty_buy_bps > 0.0 and cost_model.stamp_duty_sell_bps == 0.0
    assert cost_model.flat_charge_sell > 0.0

    no_dp_charge = cost_model.model_copy(update={"flat_charge_sell": 0.0})
    assert no_dp_charge.entry_bps() > no_dp_charge.exit_bps()

    weights = np.array([[0.0], [1.0], [0.0]])
    costs = transaction_costs(weights, cost_model, multiplier=1.0)

    assert costs[1, 0] == pytest.approx(cost_model.entry_bps() * 1e-4)
    assert costs[2, 0] == pytest.approx(cost_model.exit_bps() * 1e-4)
    assert costs.sum() == pytest.approx(cost_model.round_trip_bps() * 1e-4)


def test_a_reversal_pays_both_sides(resolved):
    weights = np.array([[1.0], [-1.0]])
    opened, closed = traded_quantities(weights)
    assert opened[1, 0] == 1.0 and closed[1, 0] == 1.0

    costs = transaction_costs(weights, resolved.cost_model, multiplier=1.0)
    expected = (resolved.cost_model.entry_bps() + resolved.cost_model.exit_bps()) * 1e-4
    assert costs[1, 0] == pytest.approx(expected)


def test_cost_multiplier_scales_linearly(resolved):
    weights = np.array([[0.0], [1.0], [0.0]])
    at_1x = transaction_costs(weights, resolved.cost_model, multiplier=1.0).sum()
    at_2x = transaction_costs(weights, resolved.cost_model, multiplier=2.0).sum()
    assert at_2x == pytest.approx(2.0 * at_1x)


# -- the lag and the exposure cap ------------------------------------------------


def test_the_lag_means_a_signal_never_earns_its_own_bar(resolved):
    """The bar the signal fires on is the bar it cannot trade. A +20% jump on
    bar 2 must not reach a strategy that only turns long on bar 2."""
    panel = PricePanel.from_frame(bars([100.0, 100.0, 120.0, 120.0]))
    signals = np.array([[0.0], [0.0], [1.0], [1.0]])

    result = run_backtest(panel, signals, resolved, cost_multiplier=0.0)

    # Bars returned are 1..3; the jump is realised on bar 2, and the position
    # only exists from bar 3 onward.
    assert result.portfolio_returns[1] == pytest.approx(0.0)
    assert result.weights[2, 0] == pytest.approx(1.0)


def test_gross_exposure_is_capped_at_one_but_never_scaled_up(resolved):
    panel = PricePanel.from_frame(bars({"AAA": [10.0] * 4, "BBB": [20.0] * 4}))

    full = np.ones((4, 2))
    capped = run_backtest(panel, full, resolved, cost_multiplier=0.0)
    assert np.abs(capped.weights).sum(axis=1).max() == pytest.approx(1.0)

    half = np.full((4, 2), 0.25)
    untouched = run_backtest(panel, half, resolved, cost_multiplier=0.0)
    assert untouched.weights[-1].tolist() == pytest.approx([0.25, 0.25])


def test_untradeable_bars_carry_no_position(resolved):
    frame = bars({"AAA": [10.0, 11.0, 12.0], "BBB": [20.0, 21.0, 22.0]})
    frame = frame.filter(~((pl.col("instrument") == "BBB") & (pl.col("date") == dt.date(2020, 1, 2))))
    panel = PricePanel.from_frame(frame)

    result = run_backtest(panel, np.ones((3, 2)), resolved, cost_multiplier=0.0)

    listed = panel.listed()[1:]
    assert (result.weights[~listed] == 0.0).all()
    assert np.isfinite(result.portfolio_returns).all()


def test_lag_and_cap_is_pure():
    signals = np.array([[1.0], [1.0]])
    lag_and_cap(signals, np.ones_like(signals, dtype=bool))
    assert signals.tolist() == [[1.0], [1.0]]


# -- fill models ----------------------------------------------------------------


def test_bar_close_fill_reduces_to_close_to_close(resolved):
    panel = PricePanel.from_frame(bars([100.0, 110.0, 121.0]))
    fills = fill_prices(panel, "bar_close")
    assert np.isnan(fills[0, 0])
    assert fills[1, 0] == 100.0

    result = run_backtest(
        panel, np.ones((3, 1)), with_fill_model(resolved, "bar_close"), cost_multiplier=0.0
    )
    # The position is established at bar 0's close, so it earns every
    # close-to-close move from bar 1 onward — the naive accounting exactly.
    assert result.portfolio_returns.tolist() == pytest.approx([0.1, 0.1])


def test_next_open_fill_earns_only_from_the_open(resolved):
    """An entry filled at the next open forgoes the overnight gap — which is
    the whole reason the model is more conservative than bar_close."""
    frame = bars({"AAA": [100.0, 120.0]}, opens={"AAA": [100.0, 110.0]})
    panel = PricePanel.from_frame(frame)
    signals = np.array([[1.0], [1.0]])

    close_fill = run_backtest(
        panel, signals, with_fill_model(resolved, "bar_close"), cost_multiplier=0.0
    )
    assert close_fill.portfolio_returns[0] == pytest.approx(0.2)

    open_fill = run_backtest(panel, signals, resolved, cost_multiplier=0.0)
    # 120/110 − 1, not 120/100 − 1: the gap from 100 to 110 belongs to whoever
    # held it overnight, and this strategy did not.
    assert open_fill.portfolio_returns[0] == pytest.approx(120.0 / 110.0 - 1.0)


def test_an_exit_at_the_next_open_gives_up_the_rest_of_the_bar(resolved):
    """The mirror of the entry case: a position closed at the open earns the
    overnight gap and nothing after it."""
    frame = bars({"AAA": [100.0, 100.0, 120.0]}, opens={"AAA": [100.0, 100.0, 110.0]})
    panel = PricePanel.from_frame(frame)
    signals = np.array([[1.0], [0.0], [0.0]])  # long from bar 1, flat from bar 2

    result = run_backtest(panel, signals, resolved, cost_multiplier=0.0)

    assert result.weights[:, 0].tolist() == pytest.approx([1.0, 0.0])
    assert result.portfolio_returns[1] == pytest.approx(110.0 / 100.0 - 1.0)


def test_queue_position_fill_is_refused_rather_than_faked(resolved):
    panel = PricePanel.from_frame(bars([100.0, 101.0]))
    with pytest.raises(FillError, match="queue position"):
        fill_prices(panel, "queue_position")


# -- drawdown and trades ---------------------------------------------------------


def test_max_drawdown_is_hand_checkable():
    returns = np.array([0.5, -0.5, 0.0])  # 1.0 -> 1.5 -> 0.75
    assert max_drawdown_from_returns(returns) == pytest.approx(0.5)
    assert max_drawdown_from_returns(np.array([])) == 0.0


def test_scaling_in_is_one_trade_and_a_flip_is_two(resolved):
    panel = PricePanel.from_frame(bars([100.0] * 8))
    signals = np.array([[0.0], [0.5], [1.0], [1.0], [0.0], [0.0], [-1.0], [-1.0]])

    result = run_backtest(panel, signals, resolved, cost_multiplier=0.0)
    trades = extract_trades(result)

    assert result.n_trades == 2
    assert len(trades) == 2
    assert [trade.side for trade in trades] == [1, -1]
    assert trades[-1].open_at_end is True


def test_artifacts_round_trip_through_parquet(tmp_path, resolved):
    panel = PricePanel.from_frame(bars([100.0, 101.0, 100.5, 102.0]))
    result = run_backtest(panel, np.ones((4, 1)), resolved)

    tradebook = write_tradebook(tmp_path / "trades.parquet", extract_trades(result))
    equity = write_equity_curve(tmp_path / "equity.parquet", result)

    assert pl.read_parquet(tradebook).height == 1
    curve = pl.read_parquet(equity)
    assert curve.height == result.n_bars
    assert curve.columns == ["date", "return", "equity", "gross_exposure", "cost"]


def test_an_empty_tradebook_still_writes_a_typed_file(tmp_path, resolved):
    panel = PricePanel.from_frame(bars([100.0, 101.0]))
    result = run_backtest(panel, np.zeros((2, 1)), resolved)
    path = write_tradebook(tmp_path / "none.parquet", extract_trades(result))
    assert pl.read_parquet(path).height == 0
