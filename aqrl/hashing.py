"""Canonical hashing — one implementation, reused everywhere.

Content hashes are load-bearing across AQRL. `market_profile_hash`,
`timeframe_profile_hash`, `cost_model_hash`, `wf_config_hash`,
`raw_content_hash`, `corporate_actions_version` and (from Stage 2) `spec_hash`
must all be *stable across processes, machines and Python runs* — TRD §6.6
exists so the day cost assumptions change we can instantly answer "which of my
40,000 stored results are still comparable?". Two hashers with subtly different
float formatting would silently partition that history.

Guarantees:

* **Key order is irrelevant.** Mappings serialise sorted by key.
* **Floats round-trip.** `repr()` is shortest-roundtrip in Python 3, so the
  same double always renders the same string.
* **Non-finite floats are rejected.** `NaN` never equals itself; hashing it
  would produce an identifier whose meaning depends on who reads it.
* **Sets are order-independent**, sorted by their own canonical form.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

__all__ = [
    "canonical_json",
    "content_hash",
    "hash_bytes",
    "hash_file",
    "hash_files",
]

_CHUNK = 1024 * 1024


def _normalise(value: Any) -> Any:
    """Reduce an arbitrary object to JSON-safe primitives, deterministically."""
    if value is None or isinstance(value, (str, bool)):
        return value

    if isinstance(value, int):  # bool is handled above; int is exact
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"cannot hash non-finite float: {value!r}")
        return value

    if isinstance(value, Decimal):
        # Decimals carry exactness a float would lose; keep the textual form.
        return f"decimal:{value.normalize():f}"

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Path):
        return value.as_posix()

    if hasattr(value, "model_dump"):  # pydantic v2 models
        return _normalise(value.model_dump(mode="python"))

    if isinstance(value, Mapping):
        return {str(k): _normalise(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}

    if isinstance(value, AbstractSet):
        return sorted(canonical_json(item) for item in value)

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalise(item) for item in value]

    if isinstance(value, (bytes, bytearray)):
        return f"sha256:{hashlib.sha256(bytes(value)).hexdigest()}"

    raise TypeError(f"unhashable type for canonical JSON: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """The canonical serialisation a content hash is taken over."""
    return json.dumps(
        _normalise(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    """sha256 of `canonical_json(value)`. The project's one content hash."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def hash_file(path: Path | str) -> str:
    """Streaming sha256 of a file's bytes — snapshots can be gigabytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def hash_files(paths: Iterable[Path | str], root: Path | str) -> str:
    """Hash a set of files as one unit, independent of traversal order.

    Used for `data_snapshots.raw_content_hash`. Each file contributes its
    path *relative to `root`* plus its own digest, so the same bytes in the
    same layout always hash identically no matter where the tree is mounted.
    """
    root_path = Path(root).resolve()
    entries = sorted(
        (Path(p).resolve().relative_to(root_path).as_posix(), hash_file(p)) for p in paths
    )
    return content_hash(entries)
