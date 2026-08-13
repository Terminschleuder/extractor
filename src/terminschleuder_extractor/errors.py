"""Exception hierarchy for the extractor.

All errors the extractor raises (and the runner catches) flow through here so
the runner can map an extraction failure to an ingestion-run ``failure``
report without leaking stack traces to the backend.
"""

from __future__ import annotations


class ExtractorError(Exception):
    """Base class for extractor-internal errors."""


class ConfigError(ExtractorError):
    """Raised when required configuration is missing/invalid at use time.

    Configuration is validated lazily: we only raise when a value is actually
    needed (e.g. an API key when calling the backend, an LLM base URL when
    extracting) so that ``--self-test`` and ``--dry-run`` work without secrets.
    """


class AuthError(ExtractorError):
    """Raised when the backend rejects or misses authentication."""


class ApiError(ExtractorError):
    """A non-2xx response from the ingestion API.

    ``status`` is the HTTP code; ``detail`` is the DRF error body (string or
    dict of field errors) for logging/diagnostics — never forwarded raw to the
    user in a way that leaks secrets.
    """

    def __init__(self, status: int, detail: object) -> None:
        super().__init__(f"API error {status}: {detail}")
        self.status = status
        self.detail = detail

    def __str__(self) -> str:
        return f"API error {self.status}: {self.detail}"


class FetchError(ExtractorError):
    """Failed to fetch or parse a source page (non-200, timeout, bad HTML)."""


class LlmError(ExtractorError):
    """The model endpoint returned an unusable response (no tool call, bad JSON)."""