"""Structured logging with structlog.

Two renderers: ``console`` (human-readable, for dev/terminal) and ``json``
(one-line JSON per event, for container log aggregation). Configured so that
**secrets are never logged**: we never log the API key, the Authorization
header, raw page payloads, or the full LLM response — only counts, ids, and
status. The logger is the single global configured here; the rest of the
package imports ``structlog.get_logger()`` and binds context (source id, run
id) per operation.
"""

from __future__ import annotations

import logging
import sys

import structlog

from .config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure structlog + stdlib logging once at process start."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Route structlog through stdlib logging so level/filter machinery applies
    # uniformly and handlers are the single sink.
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(message)s",
        force=True,
    )

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.log_format == "json":
        renderer: structlog.typing.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)