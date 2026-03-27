from __future__ import annotations

import logging
from collections import Counter

from config import AppConfig, configure_logging
from schemas import DriveFileMetadata
from utils.file_utils import json_dumps

logger = logging.getLogger(__name__)


def _is_notebook(metadata: DriveFileMetadata) -> bool:
    return metadata.file_extension.lower() == "ipynb" or metadata.mime_type == "application/vnd.google.colaboratory"


def run_pipeline() -> dict:
    from ai.indicator_extractor import IndicatorExtractor
    from ai.llm_client import LLMClient
    from ai.strategy_generator import StrategyGenerator
    from ai.summarizer import NotebookSummarizer
    from drive.auth import authenticate_accounts
    from drive.fetcher import GoogleDriveFetcher
    from engine import TradebookWriter, build_backtest_engines, load_backtest_jobs

    config = AppConfig.from_env()
    configure_logging(config.log_level)
    logger.info("Starting strategy generation and tradebook pipeline.")

    accounts = authenticate_accounts(config)
    fetcher = GoogleDriveFetcher(config, accounts)
    llm_client = LLMClient(config)
    summarizer = NotebookSummarizer(config, llm_client)
    indicator_extractor = IndicatorExtractor()
    strategy_generator = StrategyGenerator(config)
    backtest_jobs = load_backtest_jobs(config)
    backtest_engines = build_backtest_engines(config)
    primary_engine = backtest_engines[0]
    tradebook_writer = TradebookWriter(config)

    notebook_summaries = []
    scanned_files = 0
    notebook_candidates = 0
    notebooks_downloaded = 0
    download_skip_reasons: Counter[str] = Counter()

    for metadata in fetcher.iter_files():
        scanned_files += 1
        if not _is_notebook(metadata):
            continue
        notebook_candidates += 1
        logger.debug(
            "Notebook candidate file_id=%s name=%s mime_type=%s extension=%s account=%s",
            metadata.file_id,
            metadata.name,
            metadata.mime_type,
            metadata.file_extension,
            metadata.account_name,
        )

        download_result = fetcher.download_to_tempfile(metadata)
        if download_result.path is None:
            skip_reason = download_result.reason or "unknown"
            download_skip_reasons[skip_reason] += 1
            logger.warning(
                "Skipping notebook file_id=%s name=%s because download failed. reason=%s transfer_mode=%s",
                metadata.file_id,
                metadata.name,
                skip_reason,
                download_result.transfer_mode or "unknown",
            )
            continue
        notebooks_downloaded += 1
        local_path = download_result.path

        try:
            notebook_summaries.append(summarizer.summarize_notebook(metadata, local_path))
        except Exception as exc:
            logger.exception("Failed to summarize notebook %s: %s", metadata.name, exc)
        finally:
            local_path.unlink(missing_ok=True)

    if notebook_candidates and not notebooks_downloaded:
        logger.warning(
            "No notebook candidates were downloaded successfully. candidates=%d skip_reasons=%s",
            notebook_candidates,
            dict(download_skip_reasons),
        )

    summarizer.save_summaries(notebook_summaries)

    indicator_library = indicator_extractor.build_library(notebook_summaries)
    config.indicator_library_path.write_text(
        json_dumps({"indicators": [indicator.to_dict() for indicator in indicator_library]}),
        encoding="utf-8",
    )

    strategies = strategy_generator.generate(indicator_library)
    config.strategies_json_path.write_text(
        json_dumps({"strategies": [strategy.to_dict() for strategy in strategies]}),
        encoding="utf-8",
    )

    market_data_catalog = primary_engine.scan_market_data_catalog()
    market_data_catalog_summary = {
        "market_data_files_discovered": len(market_data_catalog),
        "category_counts": primary_engine.summarize_market_data_catalog(market_data_catalog),
        "datasets": [entry.to_dict() for entry in market_data_catalog],
    }
    config.market_data_catalog_path.write_text(json_dumps(market_data_catalog_summary), encoding="utf-8")

    backtest_summaries = []
    total_market_rows = 0
    for job in backtest_jobs:
        for engine in backtest_engines:
            if not engine.supports_job(job):
                continue

            market_data = engine.load_market_data(job)
            total_market_rows += int(len(market_data))
            logger.info(
                "Running backtests for job=%s engine=%s strategies=%d market_rows=%d",
                job.job_id,
                engine.engine_name,
                len(strategies),
                len(market_data),
            )
            for strategy in strategies:
                tradebook, summary = engine.run_strategy(strategy, market_data, job)
                tradebook_path = tradebook_writer.write(
                    strategy_name=strategy.strategy_name,
                    tradebook=tradebook,
                    engine_name=engine.engine_name,
                    job_id=job.job_id,
                )
                summary = tradebook_writer.attach_output_path(summary, tradebook_path)
                backtest_summaries.append(summary)

    manifest = {
        "scanned_drive_files": scanned_files,
        "notebook_candidates": notebook_candidates,
        "notebooks_downloaded": notebooks_downloaded,
        "notebooks_processed": len(notebook_summaries),
        "download_skip_reasons": dict(download_skip_reasons),
        "indicator_library_path": str(config.indicator_library_path),
        "notebook_summaries_path": str(config.notebook_summaries_path),
        "strategies_path": str(config.strategies_json_path),
        "market_data_catalog_path": str(config.market_data_catalog_path),
        "backtest_jobs_path": str(config.backtest_jobs_path),
        "market_data_files_discovered": len(market_data_catalog),
        "market_data_category_counts": market_data_catalog_summary["category_counts"],
        "tradebooks_dir": str(config.tradebooks_dir),
        "strategy_count": len(strategies),
        "backtest_job_count": len(backtest_jobs),
        "backtest_jobs": [job.to_dict() for job in backtest_jobs],
        "backtest_engine_count": len(backtest_engines),
        "backtest_engines": [engine.engine_name for engine in backtest_engines],
        "market_data_rows": total_market_rows,
        "backtest_summaries": [summary.to_dict() for summary in backtest_summaries],
    }
    config.pipeline_manifest_path.write_text(json_dumps(manifest), encoding="utf-8")

    logger.info(
        "Pipeline completed. notebooks=%d indicators=%d strategies=%d",
        len(notebook_summaries),
        len(indicator_library),
        len(strategies),
    )
    return manifest


def main() -> int:
    try:
        run_pipeline()
    except Exception as exc:
        configure_logging("INFO")
        logging.getLogger(__name__).exception("Pipeline failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
