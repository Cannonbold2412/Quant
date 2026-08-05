"""Importing this package registers every screen's routes on `server.ROUTES`
(each submodule's `@route`-decorated functions run their decorator at import
time). Order follows `UI-UX-Brief.md`'s own: Decisions -> Health -> Pipeline
-> Laboratory -> Knowledge."""
from __future__ import annotations

from . import decisions, health, knowledge, laboratory, pipeline  # noqa: F401

__all__: list[str] = []
