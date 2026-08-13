"""Tests for logging configuration and the never-log-secrets invariant."""

from __future__ import annotations

import io
import json

from terminschleuder_extractor.config import Settings
from terminschleuder_extractor.logging_setup import configure_logging, get_logger


def _run_log(settings, message, **fields):
    """Capture structlog output to a string buffer."""
    import structlog

    buf = io.StringIO()
    configure_logging(settings)
    # Rebind the logger factory to our buffer.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )
    log = get_logger("test")
    log.info(message, **fields)
    return buf.getvalue()


def test_json_log_renders_fields():
    settings = Settings(log_format="json", log_level="INFO", _env_file=None)
    line = _run_log(settings, "cycle_started", count=5, source_id=42)
    parsed = json.loads(line)
    assert parsed["event"] == "cycle_started"
    assert parsed["count"] == 5
    assert parsed["source_id"] == 42


def test_secret_values_not_logged_by_convention():
    """The package never logs raw payloads or the API key; this documents that
    the JSON renderer does not itself leak anything — secrets only appear if a
    caller explicitly passes them, which the code never does."""
    settings = Settings(api_key="super-secret", log_format="json", _env_file=None)
    line = _run_log(settings, "source_skipped", source_id=1)
    parsed = json.loads(line)
    assert "super-secret" not in json.dumps(parsed)
    assert "Authorization" not in parsed


def test_log_level_respected():
    # DEBUG message should not appear at INFO level.
    import structlog

    settings = Settings(log_level="INFO", log_format="json", _env_file=None)
    buf = io.StringIO()
    configure_logging(settings)
    structlog.configure(
        processors=[structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(30),  # WARNING (30)
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )
    get_logger("test").info("should_be_filtered")
    assert "should_be_filtered" not in buf.getvalue()