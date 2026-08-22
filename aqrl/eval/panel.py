"""The panel — one snapshot's bars as aligned `(n_bars, n_instruments)` arrays.

**Why panel-native rather than one series at a time.** Two items in the
pre-registered bar and two of the market-specific gates are cross-sectional by
definition: breadth asks *"is this edge present in more than one instrument?"*
and the equities capacity gate asks *"does the required size fit in the traded
volume?"* Neither question exists for a single series, so an engine that scores
one instrument at a time cannot answer them — it can only stub them, which is
how a gate quietly becomes decoration.

A single instrument is a panel of one column, so nothing is lost at the narrow
end.

**Built once per snapshot, sliced per fold.** TRD §9.2 names per-fold
recomputation as the single largest easy win, and across ~24 folds × 3 train
windows the same bars would otherwise be re-read, re-aligned and re-typed 72
times. `slice_dates` returns a view over the same arrays rather than a copy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl

__all__ = ["PanelError", "PricePanel"]

#: Columns the engine understands. `volume` is optional but the capacity gate
#: needs it, and its absence is reported rather than silently filled.
PANEL_COLUMNS = ("open", "high", "low", "close", "volume")
REQUIRED_PANEL_COLUMNS = ("open", "high", "low", "close")

#: The single instrument's label when a snapshot carries no `instrument` column.
SINGLE_INSTRUMENT = "__single__"


class PanelError(ValueError):
    """A frame cannot be read as a price panel."""


@dataclass(frozen=True)
class PricePanel:
    """Aligned OHLCV arrays plus the dates and instrument labels they index.

    Every array is `(n_bars, n_instruments)` and float64. A bar where an
    instrument has no data is `NaN` — never zero, never forward-filled. A
    forward fill here would invent prices on days a stock was not listed, and
    the engine would trade them.
    """

    dates: np.ndarray  # datetime64[D], shape (n_bars,)
    instruments: tuple[str, ...]
    columns: dict[str, np.ndarray]

    # -- construction ----------------------------------------------------------

    @classmethod
    def from_pandas_ohlcv(cls, frame, instrument: str = SINGLE_INSTRUMENT) -> PricePanel:
        """A single-instrument pandas OHLCV frame (DatetimeIndex) as a panel.

        The bridge nanoAQRL's synthetic generators need: they return exactly
        this shape         (`aqrl/research/synthetic_data.py`), and null-world
        calibration through the panel engine (`aqrl evaluate null-world`)
        reuses those same generators rather than duplicating them.
        """
        polars_frame = pl.from_pandas(frame.reset_index().rename(columns={"index": "date"}))
        polars_frame = polars_frame.with_columns(pl.lit(instrument).alias("instrument"))
        return cls.from_frame(polars_frame)

    @classmethod
    def from_frame(cls, frame: pl.DataFrame) -> PricePanel:
        """Pivot a long snapshot frame (`date, instrument, o/h/l/c/v`) to arrays."""
        missing = [name for name in REQUIRED_PANEL_COLUMNS if name not in frame.columns]
        if missing:
            raise PanelError(f"frame is missing required column(s) {missing}; found {frame.columns}")
        if frame.is_empty():
            raise PanelError("cannot build a panel from an empty frame")

        if "instrument" not in frame.columns:
            frame = frame.with_columns(pl.lit(SINGLE_INSTRUMENT).alias("instrument"))

        frame = frame.with_columns(pl.col("date").cast(pl.Date)).sort(["date", "instrument"])
        dates = frame.get_column("date").unique(maintain_order=False).sort()
        instruments = tuple(sorted(frame.get_column("instrument").unique().to_list()))

        date_index = {value: position for position, value in enumerate(dates.to_list())}
        instrument_index = {name: position for position, name in enumerate(instruments)}
        rows = np.array([date_index[value] for value in frame.get_column("date").to_list()])
        cols = np.array([instrument_index[name] for name in frame.get_column("instrument").to_list()])

        if np.unique(rows * len(instruments) + cols).size != rows.size:
            raise PanelError(
                "frame has more than one row for some (date, instrument) pair — "
                "a panel cannot be built from duplicated bars"
            )

        shape = (len(dates), len(instruments))
        columns: dict[str, np.ndarray] = {}
        for name in PANEL_COLUMNS:
            if name not in frame.columns:
                continue
            values = np.full(shape, np.nan, dtype=float)
            values[rows, cols] = frame.get_column(name).to_numpy().astype(float)
            columns[name] = values

        return cls(
            dates=dates.to_numpy().astype("datetime64[D]"),
            instruments=instruments,
            columns=columns,
        )

    # -- shape -----------------------------------------------------------------

    @property
    def n_bars(self) -> int:
        return int(self.dates.size)

    @property
    def n_instruments(self) -> int:
        return len(self.instruments)

    @property
    def start(self) -> date:
        return self.dates[0].astype(object)

    @property
    def end(self) -> date:
        return self.dates[-1].astype(object)

    def has(self, column: str) -> bool:
        return column in self.columns

    def column(self, name: str) -> np.ndarray:
        try:
            return self.columns[name]
        except KeyError:
            raise PanelError(
                f"panel has no column {name!r} (available: {', '.join(sorted(self.columns))})"
            ) from None

    # -- slicing ---------------------------------------------------------------

    def slice_dates(self, start: date | np.datetime64, end: date | np.datetime64) -> PricePanel:
        """The bars in `[start, end]`, as views over the same arrays.

        Inclusive at both ends: a fold's boundaries are stated in calendar time
        and a half-open window would silently drop a bar at every join.
        """
        start64 = np.datetime64(start, "D")
        end64 = np.datetime64(end, "D")
        mask = (self.dates >= start64) & (self.dates <= end64)
        return self._take(mask)

    def slice_bars(self, start: int, stop: int) -> PricePanel:
        selection = slice(start, stop)
        return PricePanel(
            dates=self.dates[selection],
            instruments=self.instruments,
            columns={name: values[selection] for name, values in self.columns.items()},
        )

    def _take(self, mask: np.ndarray) -> PricePanel:
        return PricePanel(
            dates=self.dates[mask],
            instruments=self.instruments,
            columns={name: values[mask] for name, values in self.columns.items()},
        )

    def select_instruments(self, instruments: tuple[str, ...]) -> PricePanel:
        """Narrow to a subset of instruments, preserving their panel order."""
        keep = [self.instruments.index(name) for name in instruments if name in self.instruments]
        if not keep:
            raise PanelError(f"none of {instruments} are in this panel")
        return PricePanel(
            dates=self.dates,
            instruments=tuple(self.instruments[index] for index in keep),
            columns={name: values[:, keep] for name, values in self.columns.items()},
        )

    # -- per-instrument view ---------------------------------------------------

    def instrument_columns(self, index: int) -> dict[str, np.ndarray]:
        """The 1-D columns for one instrument, as the spec compiler expects.

        `price.returns` is derived by the compiler itself from `close`, so it is
        deliberately not materialised here.
        """
        return {name: values[:, index] for name, values in self.columns.items()}

    def listed(self) -> np.ndarray:
        """`(n_bars, n_instruments)` boolean: does this instrument have a bar here?"""
        return np.isfinite(self.column("close"))
