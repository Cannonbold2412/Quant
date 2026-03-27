from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


DATASET_REGEX = re.compile(
    r"([A-Za-z0-9_\-./ ]+\.(?:csv|parquet|json|feather|xlsx))",
    flags=re.IGNORECASE,
)


def ensure_directories(paths: list[Path]) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def safe_filename(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return safe.strip("._") or "artifact"


def truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def find_dataset_references(text: str) -> list[str]:
    return sorted({match.strip() for match in DATASET_REGEX.findall(text)})


def json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=True, default=str)


def sentence_candidates(text: str) -> list[str]:
    raw_parts = re.split(r"[\r\n]+|(?<=[.!?])\s+", text)
    return [part.strip() for part in raw_parts if len(part.strip()) >= 12]
