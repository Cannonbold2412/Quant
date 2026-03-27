from __future__ import annotations

from pathlib import Path

import pandas as pd

from config import AppConfig
from schemas import StrategyBacktestSummary
from utils.file_utils import safe_filename


class TradebookWriter:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def write(self, strategy_name: str, tradebook: pd.DataFrame, engine_name: str = "default", job_id: str = "default") -> Path:
        ordered = tradebook.copy()
        if ordered.empty:
            ordered = pd.DataFrame(
                columns=[
                    "ticker",
                    "entry_time",
                    "exit_time",
                    "entry_price",
                    "exit_price",
                    "position",
                    "quantity",
                    "gross_pnl",
                    "costs",
                    "pnl",
                    "return_pct",
                    "exit_reason",
                    "cumulative_pnl",
                ]
            )
        else:
            ordered = ordered.sort_values(["exit_time", "ticker"]).reset_index(drop=True)
            ordered["cumulative_pnl"] = ordered["pnl"].cumsum().round(6)

        output_dir = self.config.tradebooks_dir / safe_filename(engine_name) / safe_filename(job_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{safe_filename(strategy_name)}.csv"
        ordered.to_csv(output_path, index=False)
        return output_path

    @staticmethod
    def attach_output_path(summary: StrategyBacktestSummary, tradebook_path: Path) -> StrategyBacktestSummary:
        summary.tradebook_path = str(tradebook_path)
        return summary
