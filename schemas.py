from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MIN_STRATEGY_INDICATORS = 3


@dataclass(slots=True)
class DriveFileMetadata:
    file_id: str
    name: str
    mime_type: str
    file_extension: str
    size: int | None
    modified_time: str | None
    parents: list[str]
    web_view_link: str | None
    drive_id: str | None
    account_name: str
    md5_checksum: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class NotebookIndicator:
    name: str
    normalized_name: str
    parameters: list[str] = field(default_factory=list)
    description: str = ""
    usage: str = ""
    contexts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class NotebookSummaryRecord:
    notebook_id: str
    account_name: str
    name: str
    modified_time: str | None
    web_view_link: str | None
    cell_count: int
    markdown_cells: int
    code_cells: int
    summary: str
    indicators: list[NotebookIndicator] = field(default_factory=list)
    indicator_names: list[str] = field(default_factory=list)
    parameters: dict[str, list[str]] = field(default_factory=dict)
    datasets: list[str] = field(default_factory=list)
    libraries: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["indicators"] = [indicator.to_dict() for indicator in self.indicators]
        return data


@dataclass(slots=True)
class IndicatorLibraryEntry:
    name: str
    parameters: list[str] = field(default_factory=list)
    description: str = ""
    usage: str = ""
    aliases: list[str] = field(default_factory=list)
    family: str = ""
    source_notebooks: list[str] = field(default_factory=list)
    mention_count: int = 0
    supported_in_engine: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class StrategyDefinition:
    strategy_name: str
    archetype: str
    indicators: list[str]
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    entry_conditions: list[str] = field(default_factory=list)
    exit_conditions: list[str] = field(default_factory=list)
    description: str = ""
    direction: str = "both"

    def __post_init__(self) -> None:
        if len(self.indicators) < MIN_STRATEGY_INDICATORS:
            raise ValueError(
                f"Strategy '{self.strategy_name}' must contain at least {MIN_STRATEGY_INDICATORS} indicators."
            )

    def signature(self) -> str:
        return f"{self.archetype}|{'|'.join(sorted(self.indicators))}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class StrategyBacktestSummary:
    strategy_name: str
    engine_name: str = ""
    job_id: str = ""
    trades: int = 0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    win_rate: float = 0.0
    average_trade_pnl: float = 0.0
    market_rows: int = 0
    tickers_tested: int = 0
    market_categories: list[str] = field(default_factory=list)
    exit_reasons: dict[str, int] = field(default_factory=dict)
    tradebook_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MarketDataCatalogEntry:
    path: str
    filename: str
    file_format: str
    inferred_category: str
    matched_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BacktestJobDefinition:
    job_id: str
    description: str = ""
    enabled: bool = True
    engine_names: list[str] = field(default_factory=list)
    market_categories: list[str] = field(default_factory=list)
    ticker_patterns: list[str] = field(default_factory=list)
    market_data_files: list[Path] = field(default_factory=list)
    market_data_dirs: list[Path] = field(default_factory=list)
    stop_loss_pct: float = 0.005
    take_profit_pct: float = 0.015
    no_entry_after: str = "15:15"
    eod_exit_time: str = "15:25"
    brokerage_rate: float = 0.001
    slippage_rate: float = 0.0001
    capital_per_trade: float = 100000.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "description": self.description,
            "enabled": self.enabled,
            "engine_names": list(self.engine_names),
            "market_categories": list(self.market_categories),
            "ticker_patterns": list(self.ticker_patterns),
            "market_data_files": [str(path) for path in self.market_data_files],
            "market_data_dirs": [str(path) for path in self.market_data_dirs],
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "no_entry_after": self.no_entry_after,
            "eod_exit_time": self.eod_exit_time,
            "brokerage_rate": self.brokerage_rate,
            "slippage_rate": self.slippage_rate,
            "capital_per_trade": self.capital_per_trade,
        }
