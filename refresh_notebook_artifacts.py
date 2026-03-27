from __future__ import annotations

import logging
from collections import Counter

from config import AppConfig, configure_logging
from schemas import DriveFileMetadata
from utils.file_utils import json_dumps

logger = logging.getLogger(__name__)


def _is_notebook(metadata: DriveFileMetadata) -> bool:
    return metadata.file_extension.lower() == "ipynb" or metadata.mime_type == "application/vnd.google.colaboratory"


def refresh_notebook_artifacts() -> dict[str, int]:
    from ai.indicator_extractor import IndicatorExtractor
    from ai.llm_client import LLMClient
    from ai.strategy_generator import StrategyGenerator
    from ai.summarizer import NotebookSummarizer
    from drive.auth import authenticate_accounts
    from drive.fetcher import GoogleDriveFetcher

    config = AppConfig.from_env()
    configure_logging(config.log_level)

    accounts = authenticate_accounts(config)
    fetcher = GoogleDriveFetcher(config, accounts)
    llm_client = LLMClient(config)
    summarizer = NotebookSummarizer(config, llm_client)
    indicator_extractor = IndicatorExtractor(config, llm_client)
    strategy_generator = StrategyGenerator(config)

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

        download_result = fetcher.download_to_tempfile(metadata)
        if download_result.path is None:
            download_skip_reasons[download_result.reason or "unknown"] += 1
            continue

        notebooks_downloaded += 1
        local_path = download_result.path
        try:
            notebook_summaries.append(summarizer.summarize_notebook(metadata, local_path))
        except Exception as exc:
            logger.exception("Failed to summarize notebook %s: %s", metadata.name, exc)
        finally:
            local_path.unlink(missing_ok=True)

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

    summary = {
        "scanned_drive_files": scanned_files,
        "notebook_candidates": notebook_candidates,
        "notebooks_downloaded": notebooks_downloaded,
        "notebooks_processed": len(notebook_summaries),
        "indicators": len(indicator_library),
        "strategies": len(strategies),
        "download_skip_reasons": len(download_skip_reasons),
    }
    logger.info("Notebook artifacts refreshed: %s", summary)
    return summary


def main() -> int:
    try:
        refresh_notebook_artifacts()
    except Exception as exc:
        configure_logging("INFO")
        logging.getLogger(__name__).exception("Notebook artifact refresh failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
