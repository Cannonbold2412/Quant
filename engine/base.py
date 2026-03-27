from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from config import AppConfig
from schemas import BacktestJobDefinition, MarketDataCatalogEntry, StrategyBacktestSummary, StrategyDefinition


class BaseBacktestEngine(ABC):
    engine_name = "base"

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def supports_job(self, job: BacktestJobDefinition) -> bool:
        return not job.engine_names or self.engine_name.lower() in {name.lower() for name in job.engine_names}

    @abstractmethod
    def load_market_data(self, job: BacktestJobDefinition | None = None) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def scan_market_data_catalog(self, job: BacktestJobDefinition | None = None) -> list[MarketDataCatalogEntry]:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def summarize_market_data_catalog(entries: list[MarketDataCatalogEntry]) -> dict[str, int]:
        raise NotImplementedError

    @abstractmethod
    def run_strategy(
        self,
        strategy: StrategyDefinition,
        market_data: pd.DataFrame,
        job: BacktestJobDefinition | None = None,
    ) -> tuple[pd.DataFrame, StrategyBacktestSummary]:
        raise NotImplementedError
