from __future__ import annotations

import json
import logging
from pathlib import Path

import nbformat

from ai.indicator_extractor import IndicatorExtractor, NOTEBOOK_CODE_CELL_SEPARATOR
from ai.llm_client import LLMClient
from config import AppConfig
from schemas import DriveFileMetadata, NotebookSummaryRecord
from utils.chunking import chunk_text
from utils.file_utils import json_dumps, sentence_candidates, truncate_text

logger = logging.getLogger(__name__)


class NotebookSummarizer:
    def __init__(self, config: AppConfig, llm_client: LLMClient | None = None) -> None:
        self.config = config
        self.llm_client = llm_client
        self.extractor = IndicatorExtractor(config, llm_client)

    def summarize_notebook(self, metadata: DriveFileMetadata, notebook_path: Path) -> NotebookSummaryRecord:
        notebook = nbformat.read(notebook_path, as_version=4)
        markdown_cells = [str(cell.source) for cell in notebook.cells if cell.cell_type == "markdown"]
        code_cells = [str(cell.source) for cell in notebook.cells if cell.cell_type == "code"]
        markdown_text = "\n\n".join(markdown_cells)
        code_text = "\n\n".join(code_cells)
        code_text_for_extraction = NOTEBOOK_CODE_CELL_SEPARATOR.join(code_cells)
        combined_text = "\n\n".join(part for part in [markdown_text, code_text] if part).strip()

        indicators, libraries, datasets, contexts = self.extractor.extract(markdown_text, code_text_for_extraction)
        summary = self._build_summary(metadata.name, combined_text, indicators, libraries, datasets, contexts)
        excerpt = truncate_text(combined_text, self.config.text_excerpt_chars)

        return NotebookSummaryRecord(
            notebook_id=metadata.file_id,
            account_name=metadata.account_name,
            name=metadata.name,
            modified_time=metadata.modified_time,
            web_view_link=metadata.web_view_link,
            cell_count=len(notebook.cells),
            markdown_cells=len(markdown_cells),
            code_cells=len(code_cells),
            summary=summary,
            indicators=indicators,
            indicator_names=sorted({indicator.normalized_name for indicator in indicators}),
            parameters={indicator.normalized_name: indicator.parameters for indicator in indicators},
            datasets=datasets,
            libraries=libraries,
            contexts=contexts[:10],
            excerpt=excerpt,
        )

    def save_summaries(self, summaries: list[NotebookSummaryRecord], path: Path | None = None) -> None:
        target_path = path or self.config.notebook_summaries_path
        target_path.write_text(json_dumps([summary.to_dict() for summary in summaries]), encoding="utf-8")

    def load_summaries(self, path: Path | None = None) -> list[NotebookSummaryRecord]:
        target_path = path or self.config.notebook_summaries_path
        if not target_path.exists():
            return []
        payload = json.loads(target_path.read_text(encoding="utf-8"))
        summaries: list[NotebookSummaryRecord] = []
        for item in payload:
            indicators = item.get("indicators", [])
            item["indicators"] = [
                self.extractor.indicator_from_dict(indicator_payload) for indicator_payload in indicators
            ]
            summaries.append(NotebookSummaryRecord(**item))
        return summaries

    def _build_summary(
        self,
        notebook_name: str,
        combined_text: str,
        indicators,
        libraries: list[str],
        datasets: list[str],
        contexts: list[str],
    ) -> str:
        chunks = chunk_text(combined_text, self.config.llm_chunk_chars)
        highlights = [self._heuristic_chunk_highlight(chunk) for chunk in chunks[:8]]
        heuristic_summary = self._heuristic_summary(notebook_name, indicators, libraries, datasets, contexts, highlights)

        if self.llm_client and self.llm_client.available:
            try:
                return self._llm_summary(notebook_name, indicators, libraries, datasets, contexts, highlights, heuristic_summary)
            except Exception as exc:  # pragma: no cover
                logger.warning("Falling back to heuristic notebook summary for %s: %s", notebook_name, exc)
        return heuristic_summary

    @staticmethod
    def _heuristic_chunk_highlight(chunk: str) -> str:
        for sentence in sentence_candidates(chunk):
            lowered = sentence.lower()
            if any(token in lowered for token in ["entry", "exit", "signal", "indicator", "backtest", "strategy"]):
                return sentence
        return truncate_text(chunk.replace("\n", " "), 220)

    @staticmethod
    def _heuristic_summary(
        notebook_name: str,
        indicators,
        libraries: list[str],
        datasets: list[str],
        contexts: list[str],
        highlights: list[str],
    ) -> str:
        indicator_names = [indicator.normalized_name for indicator in indicators]
        indicator_text = ", ".join(indicator_names[:6]) if indicator_names else "no recognized indicators"
        library_text = ", ".join(libraries[:4]) if libraries else "plain Python or pandas tooling"
        dataset_text = ", ".join(datasets[:3]) if datasets else "no explicit dataset path"
        context_text = contexts[0] if contexts else (highlights[0] if highlights else "")
        parts = [
            f"{notebook_name} uses {indicator_text}.",
            f"Detected libraries: {library_text}.",
            f"Referenced datasets: {dataset_text}.",
        ]
        if context_text:
            parts.append(f"Representative usage context: {context_text}")
        return " ".join(parts)

    def _llm_summary(
        self,
        notebook_name: str,
        indicators,
        libraries: list[str],
        datasets: list[str],
        contexts: list[str],
        highlights: list[str],
        heuristic_summary: str,
    ) -> str:
        payload = {
            "notebook_name": notebook_name,
            "heuristic_summary": heuristic_summary,
            "indicators": [indicator.to_dict() for indicator in indicators],
            "libraries": libraries,
            "datasets": datasets,
            "contexts": contexts[:8],
            "chunk_highlights": highlights[:8],
        }
        system_prompt = (
            "You summarize quantitative research notebooks into concise structured memory. "
            "Preserve indicator usage, parameters, and notebook intent. Return only valid JSON."
        )
        user_prompt = (
            "Return JSON with exactly one key named summary. "
            "The summary must be 2-4 sentences and grounded only in the provided payload.\n\n"
            f"{json.dumps(payload, indent=2, ensure_ascii=True)}"
        )
        response = self.llm_client.generate_json(system_prompt, user_prompt)
        return str(response.get("summary") or heuristic_summary)
