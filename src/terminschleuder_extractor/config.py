"""Configuration via environment (``EXTRACTOR_*``) and an optional ``.env`` file.

Uses pydantic-settings. Validation is **lazy**: missing secrets only raise when
they are actually needed, so ``--self-test`` and ``--dry-run`` (which never call
the LLM and, for self-test, never call the API) work without any secrets set.

Cadence is controlled by two independent knobs (see ``runner.py``):

* ``poll_interval_seconds`` — how often the runner wakes up to ask the backend
  for due sources (default 1h).
* ``min_source_interval_seconds`` — a client-side per-source politeness floor:
  never crawl the same source more often than this, even if the backend reports
  it due and the cycle is shorter (default 1h). A 5-minute cycle with the floor
  at 3600s still crawls each site at most once per hour.

The backend's own ``/sources/due/`` endpoint is the *primary* per-source
cadence (it only returns sources whose ``next_due_at`` has passed); the
client-side floor is a belt-and-suspenders guard against hammering one site.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EXTRACTOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Ingestion API ---
    api_base_url: str = "https://www.terminschleuder.online"
    api_key: SecretStr = SecretStr("")

    # --- Runner cadence ---
    run_mode: Literal["loop", "once"] = "loop"
    poll_interval_seconds: int = Field(3600, ge=0)
    min_source_interval_seconds: int = Field(3600, ge=0)
    max_sources_per_cycle: int = Field(20, ge=1)
    # Optional path to persist last-crawled timestamps across restarts.
    state_file: str = ""

    # --- HTTP / page ---
    http_timeout_seconds: float = Field(30.0, gt=0)
    user_agent: str = "terminschleuder-extractor/0.1"
    max_page_chars: int = Field(20000, ge=1000)
    # Max per-event detail pages to follow per source. When a source page is a
    # listing that links to individual event pages, the extractor fetches each
    # detail page to recover time-of-day / full venue / description that only
    # appear there. 0 disables detail following (listing-only extraction).
    max_detail_pages_per_source: int = Field(10, ge=0)

    # --- OpenAI-compatible LLM endpoint ---
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: SecretStr = SecretStr("ollama")
    llm_model: str = "llama3.1"
    llm_max_tokens: int = Field(4096, ge=1)
    llm_temperature: float = Field(0.0, ge=0.0, le=2.0)

    # --- Behavior flags (also settable via CLI) ---
    dry_run: bool = False

    # --- Logging ---
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    # --- Convenience ---
    def require_api_key(self) -> str:
        """Return the raw API key or raise ``ConfigError`` if unset."""
        value = self.api_key.get_secret_value()
        if not value:
            from .errors import ConfigError

            raise ConfigError("EXTRACTOR_API_KEY is not set.")
        return value

    def require_llm_api_key(self) -> str:
        """Return the raw LLM API key (Ollama ignores it; others require it)."""
        return self.llm_api_key.get_secret_value() or "ollama"