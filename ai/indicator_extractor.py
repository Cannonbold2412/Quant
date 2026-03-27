from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from schemas import IndicatorLibraryEntry, NotebookIndicator, NotebookSummaryRecord
from utils.chunking import chunk_text
from utils.file_utils import find_dataset_references, truncate_text
from utils.indicator_registry import (
    BOOTSTRAP_INDICATORS,
    INDICATOR_REGISTRY,
    canonicalize_indicator_name,
    indicator_metadata,
)

if TYPE_CHECKING:
    from ai.llm_client import LLMClient
    from config import AppConfig

logger = logging.getLogger(__name__)

NOTEBOOK_CODE_CELL_SEPARATOR = "\n\n# === NOTEBOOK CELL BREAK ===\n\n"

LIBRARY_PATTERNS: dict[str, str] = {
    "pandas": r"\bimport\s+pandas\b|\bpd\.",
    "numpy": r"\bimport\s+numpy\b|\bnp\.",
    "pandas_ta": r"\bimport\s+pandas_ta\b|\bta\.",
    "ta-lib": r"\bimport\s+talib\b|\btalib\.",
    "vectorbt": r"\bvectorbt\b",
    "backtrader": r"\bbacktrader\b",
    "plotly": r"\bplotly\b",
    "matplotlib": r"\bmatplotlib\b|\bplt\.",
}

PARAMETER_NAME_MAP: dict[str, str] = {
    "length": "period",
    "len": "period",
    "timeperiod": "period",
    "window": "window",
    "span": "window",
    "std": "num_std",
    "nbdevup": "num_std",
    "nbdevdn": "num_std",
    "signalperiod": "signal_window",
    "fastperiod": "fast_window",
    "slowperiod": "slow_window",
}

class IndicatorExtractor:
    def __init__(self, config: AppConfig | None = None, llm_client: LLMClient | None = None) -> None:
        self.config = config
        self.llm_client = llm_client

    def extract(
        self,
        markdown_text: str,
        code_text: str,
    ) -> tuple[list[NotebookIndicator], list[str], list[str], list[str]]:
        datasets = find_dataset_references("\n".join(part for part in [markdown_text, code_text] if part))
        libraries = self._extract_libraries(markdown_text, code_text)

        indicators = self._extract_indicators_with_llm(markdown_text, code_text)

        contexts: list[str] = []
        for indicator in indicators:
            contexts.extend(indicator.contexts)
        return indicators, libraries, datasets, list(dict.fromkeys(contexts))

    @staticmethod
    def indicator_from_dict(payload: dict[str, Any]) -> NotebookIndicator:
        return NotebookIndicator(**payload)

    def build_library(self, summaries: list[NotebookSummaryRecord]) -> list[IndicatorLibraryEntry]:
        merged: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "parameters": set(),
                "usage": [],
                "aliases": set(),
                "source_notebooks": set(),
                "mention_count": 0,
                "descriptions": [],
            }
        )

        for summary in summaries:
            for indicator in summary.indicators:
                entry = merged[indicator.normalized_name]
                entry["parameters"].update(indicator.parameters)
                if indicator.usage:
                    entry["usage"].append(indicator.usage)
                if indicator.description:
                    entry["descriptions"].append(indicator.description)
                entry["aliases"].add(indicator.name)
                entry["source_notebooks"].add(summary.name)
                entry["mention_count"] += max(1, len(indicator.contexts))

        supported_count = sum(1 for name in merged if indicator_metadata(name).get("supported_in_engine"))
        if supported_count < 6:
            for name in BOOTSTRAP_INDICATORS:
                entry = merged[name]
                entry["usage"].append(
                    "Bootstrap baseline added because extracted notebook coverage was too sparse for broad strategy generation."
                )

        library: list[IndicatorLibraryEntry] = []
        for name, payload in sorted(merged.items(), key=lambda item: (-item[1]["mention_count"], item[0])):
            metadata = indicator_metadata(name)
            usage = self._merge_usage_examples(payload["usage"])
            description = self._merge_descriptions(payload["descriptions"], metadata["description"])
            library.append(
                IndicatorLibraryEntry(
                    name=name,
                    parameters=sorted(payload["parameters"]) or self._default_parameter_strings(name),
                    description=description,
                    usage=usage,
                    aliases=sorted({alias for alias in payload["aliases"] if alias and alias.lower() != name}),
                    family=metadata["family"],
                    source_notebooks=sorted(payload["source_notebooks"]),
                    mention_count=int(payload["mention_count"]),
                    supported_in_engine=bool(metadata["supported_in_engine"]),
                )
            )
        return library

    def _extract_indicators_with_llm(self, markdown_text: str, code_text: str) -> list[NotebookIndicator]:
        if not self.llm_client or not self.llm_client.available:
            return []

        notebook_text = self._format_notebook_for_llm(markdown_text, code_text)
        if not notebook_text.strip():
            return []

        client_index = self.llm_client.reserve_client()
        chunk_size = self.config.llm_chunk_chars if self.config else 7000
        chunks = self._llm_chunks(notebook_text, chunk_size)
        merged: dict[str, dict[str, Any]] = {}

        try:
            for chunk_number, chunk in enumerate(chunks, start=1):
                payload = self._extract_llm_chunk(
                    chunk=chunk,
                    chunk_number=chunk_number,
                    total_chunks=len(chunks),
                    client_index=client_index,
                )
                self._merge_llm_payload(merged, payload.get("indicators", []))
        except Exception as exc:
            logger.warning("LLM indicator extraction failed: %s", exc)
            return []

        indicators: list[NotebookIndicator] = []
        for normalized_name, payload in sorted(merged.items()):
            metadata = indicator_metadata(normalized_name)
            contexts = list(dict.fromkeys(payload["contexts"]))[:5]
            description = payload["description"] or metadata["description"]
            usage = payload["usage"] or (contexts[0] if contexts else description)
            indicators.append(
                NotebookIndicator(
                    name=payload["name"] or normalized_name,
                    normalized_name=normalized_name,
                    parameters=sorted(payload["parameters"]),
                    description=description,
                    usage=usage,
                    contexts=contexts,
                )
            )
        return indicators

    def _extract_llm_chunk(
        self,
        chunk: str,
        chunk_number: int,
        total_chunks: int,
        client_index: int,
    ) -> dict[str, Any]:
        system_prompt = "Return only valid JSON."
        user_prompt = (
            "Extract all the trading indicators from this code in this schema:\n"
            "{\n"
            '  "indicators": [\n'
            "    {\n"
            '      "name": "string",\n'
            '      "normalized_name": "string",\n'
            '      "parameters": ["key=value"],\n'
            '      "description": "string",\n'
            '      "usage": "string",\n'
            '      "contexts": ["string"]\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            f"{chunk}"
        )
        payload = self.llm_client.generate_json(system_prompt, user_prompt, client_index=client_index)
        if not isinstance(payload, dict):
            return {"indicators": []}
        indicators = payload.get("indicators")
        if not isinstance(indicators, list):
            return {"indicators": []}
        return {"indicators": indicators}

    @staticmethod
    def _format_notebook_for_llm(markdown_text: str, code_text: str) -> str:
        if code_text.strip():
            return code_text.strip()
        return markdown_text.strip()

    @staticmethod
    def _llm_chunks(notebook_text: str, chunk_size: int) -> list[str]:
        if NOTEBOOK_CODE_CELL_SEPARATOR in notebook_text:
            return [chunk.strip() for chunk in notebook_text.split(NOTEBOOK_CODE_CELL_SEPARATOR) if chunk.strip()]
        return chunk_text(notebook_text, chunk_size)

    def _merge_llm_payload(self, accumulator: dict[str, dict[str, Any]], candidates: list[Any]) -> None:
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            normalized_name = canonicalize_indicator_name(
                str(candidate.get("normalized_name") or candidate.get("name") or "").strip()
            )
            if not normalized_name:
                continue

            payload = accumulator.setdefault(
                normalized_name,
                {"name": "", "parameters": set(), "description": "", "usage": "", "contexts": []},
            )

            raw_name = str(candidate.get("name") or normalized_name).strip()
            display_name = self._display_name(raw_name, normalized_name)
            if display_name and not payload["name"]:
                payload["name"] = display_name

            parameters = self._normalize_parameter_strings(candidate.get("parameters"))
            payload["parameters"].update(parameters)

            description = truncate_text(str(candidate.get("description") or "").strip(), 220)
            if description and (not payload["description"] or payload["description"].startswith("Custom indicator")):
                payload["description"] = description

            usage = truncate_text(str(candidate.get("usage") or "").strip(), 220)
            if usage and not payload["usage"]:
                payload["usage"] = usage

            contexts = candidate.get("contexts") if isinstance(candidate.get("contexts"), list) else []
            if usage:
                payload["contexts"].append(usage)
            for context in contexts:
                if not isinstance(context, str):
                    continue
                cleaned = truncate_text(context.strip(), 220)
                if cleaned:
                    payload["contexts"].append(cleaned)

    def _extract_libraries(self, markdown_text: str, code_text: str) -> list[str]:
        combined = "\n".join([markdown_text, code_text])
        libraries = [name for name, pattern in LIBRARY_PATTERNS.items() if re.search(pattern, combined, flags=re.IGNORECASE)]
        return sorted(libraries)

    @staticmethod
    def _normalize_parameter_strings(raw_parameters: Any) -> list[str]:
        if not isinstance(raw_parameters, list):
            return []
        parameters: set[str] = set()
        for parameter in raw_parameters:
            if not isinstance(parameter, str):
                continue
            candidate = parameter.strip()
            if not candidate:
                continue
            if "=" in candidate:
                key, value = candidate.split("=", 1)
                normalized_key = PARAMETER_NAME_MAP.get(key.strip().lower(), key.strip().lower())
                parameters.add(f"{normalized_key}={value.strip()}")
            else:
                parameters.add(candidate)
        return sorted(parameters)

    @staticmethod
    def _display_name(raw_name: str, normalized_name: str) -> str:
        raw_name = raw_name.strip()
        if not raw_name:
            return normalized_name
        raw_slug = canonicalize_indicator_name(raw_name) or ""
        if normalized_name in INDICATOR_REGISTRY:
            if raw_name.isupper() or raw_slug == normalized_name:
                return raw_name
            if raw_slug.startswith(f"{normalized_name}_") or raw_slug.endswith(f"_{normalized_name}"):
                return normalized_name
            return normalized_name
        return raw_name

    @staticmethod
    def _merge_usage_examples(examples: list[str]) -> str:
        unique = list(dict.fromkeys(example for example in examples if example))
        if not unique:
            return ""
        return " | ".join(unique[:3])

    @staticmethod
    def _merge_descriptions(descriptions: list[str], fallback: str) -> str:
        unique = [description for description in dict.fromkeys(descriptions) if description]
        return unique[0] if unique else fallback

    @staticmethod
    def _default_parameter_strings(name: str) -> list[str]:
        defaults = indicator_metadata(name)["default_parameters"]
        return [f"{key}={value}" for key, value in defaults.items()]
