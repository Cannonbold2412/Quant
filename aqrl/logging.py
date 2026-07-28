"""Structured logging with correlation IDs threading strategy -> experiment -> job.

Implementation_Plan §3 and TRD §17 both ask for exactly one thing: when
something goes wrong three stages from now, it must be possible to pull every
line belonging to one piece of research out of a shared log. That means the
identifiers travel *with the execution context*, not as arguments every call
site has to remember to pass.

    with log_context(strategy_id=7):
        log.info("spec accepted")            # carries strategy_id
        with log_context(experiment_id=41):
            log.info("evaluating")           # carries both, plus correlation_id

`correlation_id` is minted automatically by the outermost block, so a single
CLI invocation or worker job is greppable end to end.

Note on the module name: absolute imports are the default in Python 3, so
`import logging` inside this file resolves to the standard library, not here.
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

__all__ = ["configure", "current_context", "get_logger", "log_context"]

# `None` rather than `{}`: a mutable default on a ContextVar is shared across
# every context that never set one, so an accidental in-place update would leak
# one job's correlation IDs into another's.
_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("aqrl_log_context", default=None)

_EMPTY: dict[str, Any] = {}

# Attributes the stdlib puts on every LogRecord. Anything else came from
# `extra=` and belongs in the structured payload.
_STANDARD_ATTRS = frozenset(
    ["args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName", "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name", "pathname", "process", "processName", "relativeCreated", "stack_info", "taskName", "thread", "threadName"]
)

_configured = False


def current_context() -> dict[str, Any]:
    """The correlation fields in effect right now."""
    return dict(_CONTEXT.get() or _EMPTY)


@contextmanager
def log_context(**fields: Any) -> Iterator[dict[str, Any]]:
    """Bind correlation fields for the duration of the block.

    Nested blocks merge; the outermost one mints a `correlation_id` if the
    caller did not supply one.
    """
    parent = _CONTEXT.get() or _EMPTY
    merged = {**parent, **{k: v for k, v in fields.items() if v is not None}}
    if "correlation_id" not in merged:
        merged["correlation_id"] = uuid.uuid4().hex
    token = _CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _CONTEXT.reset(token)


def _record_payload(record: logging.LogRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "message": record.getMessage(),
    }
    payload.update(_CONTEXT.get() or _EMPTY)
    for key, value in record.__dict__.items():
        if key not in _STANDARD_ATTRS and not key.startswith("_"):
            payload[key] = value
    if record.exc_info:
        payload["exception"] = logging.Formatter().formatException(record.exc_info)
    return payload


class JsonFormatter(logging.Formatter):
    """One JSON object per line — greppable by machine."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_record_payload(record), default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """The same payload, laid out for a human reading a terminal."""

    def format(self, record: logging.LogRecord) -> str:
        payload = _record_payload(record)
        head = f"{payload.pop('ts')} {payload.pop('level'):<7} {payload.pop('logger')}"
        message = payload.pop("message")
        exception = payload.pop("exception", None)
        extras = " ".join(f"{k}={v}" for k, v in payload.items())
        line = f"{head} {message}" + (f"  [{extras}]" if extras else "")
        return f"{line}\n{exception}" if exception else line


def configure(level: str | None = None, log_format: str | None = None, *, force: bool = False) -> None:
    """Install the handler. Idempotent unless `force=True`.

    Defaults come from `aqrl.config.Settings`; explicit arguments win, so a
    test or a `--verbose` flag can override without touching the environment.
    """
    global _configured
    if _configured and not force:
        return

    from .config import get_settings

    settings = get_settings()
    resolved_level = (level or settings.log_level).upper()
    resolved_format = (log_format or settings.log_format).lower()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if resolved_format == "json" else TextFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved_level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """A configured logger. Safe to call at import time."""
    configure()
    return logging.getLogger(name)
