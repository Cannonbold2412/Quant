"""Outbound HTTP — the first in this codebase (everything else is a vendor
SDK: `anthropic`, `voyageai`). Same split as `agents/session.py` and
`agents/embeddings.py`, for the same reason: a `Protocol`, one real
implementation behind stdlib `urllib`, and a `Stub*` so no test in this
codebase ever touches the network.

**No new dependency.** `urllib.request` covers everything the collectors
need — arXiv's API and RSS/Atom feeds are just XML/HTML over plain GET, and
this package's own `chunking`/`html_text`/`relevance` modules are already
stdlib-only. `httpx`/`feedparser`/`beautifulsoup4` would each replace a few
dozen lines here with a new dependency for work that does not need one.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Protocol

from ..config import get_settings

__all__ = ["FetchError", "Fetcher", "StubFetcher", "UrllibFetcher"]


class FetchError(RuntimeError):
    """A `Fetcher.get` call failed — network error or non-2xx status."""


class Fetcher(Protocol):
    def get(self, url: str, *, accept: str | None = None) -> bytes:
        """Fetch `url`'s body. Raises `FetchError` on failure. Stateless."""
        ...


class UrllibFetcher:
    """The real fetcher. One process-wide `_last_request_at` per host, so a
    collector sweeping many arXiv hits in a loop never exceeds
    `Settings.librarian_min_request_interval_seconds` against the same
    host — polite-by-default rather than relying on every collector to
    remember to throttle itself.
    """

    def __init__(self, *, timeout_seconds: float | None = None, user_agent: str | None = None) -> None:
        settings = get_settings()
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.http_timeout_seconds
        self.user_agent = user_agent or settings.http_user_agent
        self._min_interval = settings.librarian_min_request_interval_seconds
        self._last_request_at: dict[str, float] = {}

    def _throttle(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            wait = self._min_interval - elapsed
            if wait > 0:
                time.sleep(wait)
        self._last_request_at[host] = time.monotonic()

    def get(self, url: str, *, accept: str | None = None) -> bytes:
        from urllib.parse import urlparse

        self._throttle(urlparse(url).netloc)
        headers = {"User-Agent": self.user_agent}
        if accept:
            headers["Accept"] = accept
        request = urllib.request.Request(url, headers=headers)  # noqa: S310 - collectors' own fetch surface
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchError(f"GET {url} failed: {exc}") from exc


class StubFetcher:
    """Returns pre-supplied bytes keyed by exact URL. No network, ever —
    the default for every test in this codebase, same role `StubSession`
    plays for `agents/session.py`."""

    def __init__(self, responses: dict[str, bytes]) -> None:
        self._responses = responses
        self.requested: list[str] = []

    def get(self, url: str, *, accept: str | None = None) -> bytes:
        self.requested.append(url)
        if url not in self._responses:
            raise FetchError(f"StubFetcher has no response configured for {url!r}")
        return self._responses[url]
