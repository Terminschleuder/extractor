"""Tests for Settings (env-driven config)."""

from __future__ import annotations

import pytest

from terminschleuder_extractor.config import Settings
from terminschleuder_extractor.errors import ConfigError


def test_defaults():
    s = Settings(_env_file=None)
    assert s.run_mode == "loop"
    assert s.poll_interval_seconds == 3600
    assert s.min_source_interval_seconds == 3600
    assert s.llm_base_url == "http://localhost:11434/v1"
    assert s.llm_model == "llama3.1"
    assert s.llm_temperature == 0.0


def test_env_prefix(monkeypatch):
    monkeypatch.setenv("EXTRACTOR_API_KEY", "secret-key")
    monkeypatch.setenv("EXTRACTOR_LLM_MODEL", "qwen2.5")
    monkeypatch.setenv("EXTRACTOR_POLL_INTERVAL_SECONDS", "300")
    s = Settings(_env_file=None)
    assert s.api_key.get_secret_value() == "secret-key"
    assert s.llm_model == "qwen2.5"
    assert s.poll_interval_seconds == 300


def test_require_api_key_raises_when_unset():
    s = Settings(_env_file=None)
    with pytest.raises(ConfigError):
        s.require_api_key()


def test_require_api_key_returns_value_when_set():
    s = Settings(api_key="abc", _env_file=None)
    assert s.require_api_key() == "abc"


def test_llm_api_key_defaults_to_ollama():
    s = Settings(_env_file=None)
    # Ollama ignores the key; default keeps a non-empty placeholder.
    assert s.require_llm_api_key() == "ollama"


def test_run_mode_literal_validated():
    with pytest.raises(Exception):
        Settings(run_mode="bogus", _env_file=None)