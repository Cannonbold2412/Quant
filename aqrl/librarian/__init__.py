"""The Librarian & Curiosity Engine (Implementation_Plan §13, TRD §12.2).

Pure Python collectors and chunking/relevance helpers live here, kept
separate from the Claude session wrapper (`agents/session.py`) and the job
handler (`orchestration/handlers/librarian.py`) the same way `aqrl/eval/`
is separate from the handler that drives it — this package has no database
or job-queue dependency of its own.
"""

from .chunking import Chunk, chunk_by_structure
from .collectors import ArxivCollector, CollectedDoc, Collector, FeedCollector
from .fetch import FetchError, Fetcher, StubFetcher, UrllibFetcher
from .html_text import html_to_text
from .relevance import score as relevance_score

__all__ = [
    "ArxivCollector",
    "Chunk",
    "CollectedDoc",
    "Collector",
    "FeedCollector",
    "FetchError",
    "Fetcher",
    "StubFetcher",
    "UrllibFetcher",
    "chunk_by_structure",
    "html_to_text",
    "relevance_score",
]
