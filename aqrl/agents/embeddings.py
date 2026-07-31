"""The embedding provider — real (Voyage AI) or deterministic-offline, for
Stage 7's Research Brief relevance search (Implementation_Plan §10,
`research_brief.py`).

**Same split as `session.py`, for the same reason.** Anthropic has no
embeddings endpoint, so relevance search needs a second provider — but every
test in this codebase, including the full offline loop test, must still run
without any network credential. `VoyageEmbedder` is the only implementation
that touches the network; `StubEmbedder` returns a deterministic,
content-derived vector so cosine similarity is stable across runs without a
`VOYAGE_API_KEY`.

**Why not `numpy.random` for the stub.** A stub whose vectors are random
per-call would make `top_k_relevant`'s ranking flaky between test runs. Each
text is instead hashed (`aqrl.hashing.hash_bytes`, the project's one content
hash) into a fixed-dimension vector — same text, same vector, forever, with
no seed to manage and no dependency on call order.
"""
from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence
from typing import Any, Protocol

from ..config import get_settings

__all__ = ["Embedder", "StubEmbedder", "VoyageEmbedder", "embedding_model_name"]

#: The stub's vector width. Arbitrary but fixed — cosine similarity only
#: requires every vector in a comparison to share a dimension, and the stub
#: never needs to agree with a real model's actual embedding space.
_STUB_DIMENSIONS = 32

#: Recorded on every `embeddings.model` row, so a query embedded under one
#: model is never compared against vectors cached under another. Voyage's
#: current general-purpose text model — not hand-derived from anything
#: version-sensitive, so bumping it is a config change, not a schema one.
_VOYAGE_MODEL = "voyage-3.5"


def embedding_model_name(embedder: Embedder) -> str:
    """The identity string stamped on cached vectors for this embedder.

    Kept alongside the protocol (not a method on it) so a stub and the real
    client don't have to agree on an interface neither strictly needs.
    """
    if isinstance(embedder, VoyageEmbedder):
        return embedder.model
    return "stub-hash-v1"


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per input text, same order. Stateless."""
        ...


class StubEmbedder:
    """Deterministic, offline, no network — every text hashes to the same
    fixed-dimension vector every time. The default for every test."""

    def __init__(self, dimensions: int = _STUB_DIMENSIONS) -> None:
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        needed_bytes = self.dimensions * 4  # one float32 per dimension
        raw = b""
        counter = 0
        while len(raw) < needed_bytes:
            raw += hashlib.sha256(f"{counter}:{text}".encode()).digest()
            counter += 1
        # Unsigned 32-bit words -> [-1, 1] floats: cheap, stable, no numpy
        # dependency needed just to unpack bytes.
        words = struct.unpack(f">{self.dimensions}I", raw[:needed_bytes])
        return [(word / 0xFFFFFFFF) * 2.0 - 1.0 for word in words]


class VoyageEmbedder:
    """The real embedding client. Imports `voyageai` lazily — same reasoning
    as `AnthropicSession._get_client` (`agents/session.py`): nothing that
    never constructs this class needs the package installed or an API key.
    """

    def __init__(self, *, model: str | None = None, client: Any = None) -> None:
        self.model = model or _VOYAGE_MODEL
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        import voyageai

        settings = get_settings()
        api_key = getattr(settings, "voyage_api_key", None)
        self._client = voyageai.Client(api_key=api_key) if api_key else voyageai.Client()
        return self._client

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._get_client()
        result = client.embed(list(texts), model=self.model, input_type="document")
        return list(result.embeddings)
