"""Shared pytest fixtures for the extractor test suite.

Tests never touch the network or a real model: httpx is mocked with respx
and the OpenAI client is replaced with a fake. No secrets are required.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from terminschleuder_extractor.config import Settings
from terminschleuder_extractor.models import DueSource, OrganizationMini


def _settings(**overrides: Any) -> Settings:
    """Build Settings with the api key set and ``**overrides`` applied."""
    base = {
        "api_key": "test-api-key",
        "llm_api_key": "test-llm-key",
        "llm_model": "test-model",
        "min_source_interval_seconds": 3600,
        "poll_interval_seconds": 3600,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings_factory():
    return _settings


@pytest.fixture
def settings():
    return _settings()


@pytest.fixture
def sample_source() -> DueSource:
    return DueSource(
        id=42,
        organization=OrganizationMini(id=7, name="Acme", slug="acme"),
        url="https://example.com/events",
        platform="llm",
        fetch_interval_minutes=60,
        last_fetched_at=None,
        next_due_at=None,
    )


@pytest.fixture
def due_source_payload():
    """A single-page due-sources response (DRF pagination shape)."""
    return {
        "count": 1,
        "next": None,
        "previous": None,
        "results": [
            {
                "id": 42,
                "organization": {"id": 7, "name": "Acme", "slug": "acme"},
                "url": "https://example.com/events",
                "platform": "llm",
                "fetch_interval_minutes": 60,
                "last_fetched_at": None,
                "next_due_at": None,
            }
        ],
    }


def make_event(**overrides: Any) -> dict[str, Any]:
    """A minimal valid event dict as the LLM tool would return it."""
    base = {
        "title": "PyGraten",
        "starts_at": "2025-09-15T19:00:00",
    }
    base.update(overrides)
    return base


@pytest.fixture
def make_tool_response():
    """Build a fake OpenAI chat completion response object.

    Returns a callable: ``make_tool_response(events=[...], content=None)``
    mimics ``openai.ChatCompletion`` with ``choices[0].message.tool_calls[0]
    .function.arguments`` set to JSON, or (if events is None) a content-only
    response for the fallback path.
    """

    def factory(events: list[dict] | None = None, content: str | None = None) -> Any:
        tool_calls = None
        if events is not None:
            tool_calls = [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "extract_events",
                        "arguments": json.dumps({"events": events}),
                    },
                }
            ]
        message = {"content": content, "tool_calls": tool_calls}

        class _Resp:
            choices = [{"message": message, "finish_reason": "tool_calls"}]

        return _Resp()

    return factory


@pytest.fixture
def fixed_now():
    return datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def event_dict():
    return make_event