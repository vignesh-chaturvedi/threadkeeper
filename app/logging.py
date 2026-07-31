"""Structured JSON logging with a conversation_id bound to every line.

The point of this file: once a conversation_id is bound to the context, every
log line emitted anywhere downstream carries it — including lines from code that
has no idea a conversation exists. Debugging becomes `jq 'select(.conversation_id
== "...")'` instead of grep archaeology across interleaved async tasks.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, unbind_contextvars

from app.settings import get_settings

_configured = False


def _scrub_pii(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    """No identifier reaches a log line, ever.

    By the time anything is logged the text should already be tokenized. This is
    the belt to that braces: a log statement added in a hurry is exactly how a
    PAN ends up in a log aggregator forever, and the aggregator has a longer
    retention policy than anyone remembers.
    """
    from app.privacy.patterns import scrub

    for key, value in event.items():
        if isinstance(value, str) and len(value) >= 10:
            event[key] = scrub(value)
    return event


def configure_logging(force: bool = False) -> None:
    global _configured
    if _configured and not force:
        return

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,  # <- pulls conversation_id in
        structlog.stdlib.add_log_level,
        # NB: not structlog.stdlib.add_logger_name — that processor reads
        # `logger.name`, which only exists on stdlib loggers. We render through
        # PrintLoggerFactory, so get_logger() binds the module name instead.
        _scrub_pii,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.log_format == "json":
        renderer: Any = structlog.processors.JSONRenderer()
        shared.append(structlog.processors.format_exc_info)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
        shared.append(structlog.processors.ExceptionPrettyPrinter())

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib loggers (uvicorn, alembic, psycopg) through the same renderer
    # so the output stream is uniformly parseable.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "alembic", "psycopg"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    _configured = True


def get_logger(name: str | None = None) -> Any:
    configure_logging()
    logger = structlog.get_logger()
    return logger.bind(logger=name) if name else logger


@contextmanager
def log_context(**kwargs: Any) -> Iterator[None]:
    """Bind fields for the duration of a block, then restore.

    Used at the two entry points that matter: the webhook handler and the
    scheduler's per-job loop.
    """
    bind_contextvars(**kwargs)
    try:
        yield
    finally:
        unbind_contextvars(*kwargs.keys())


__all__ = [
    "bind_contextvars",
    "clear_contextvars",
    "configure_logging",
    "get_logger",
    "log_context",
]
