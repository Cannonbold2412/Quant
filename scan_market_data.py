from __future__ import annotations

from config import AppConfig, configure_logging
from engine.backtester import BacktestEngine
from utils.file_utils import json_dumps


def main() -> int:
    config = AppConfig.from_env()
    configure_logging(config.log_level)

    backtester = BacktestEngine(config)
    catalog = backtester.scan_market_data_catalog()
    payload = {
        "market_data_files_discovered": len(catalog),
        "category_counts": backtester.summarize_market_data_catalog(catalog),
        "datasets": [entry.to_dict() for entry in catalog],
    }
    config.market_data_catalog_path.write_text(json_dumps(payload), encoding="utf-8")
    print(json_dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
