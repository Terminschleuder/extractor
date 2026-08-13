"""Tests for the LLM extractor: HTML fetch, cleaning, tool-call + fallback parsing."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from terminschleuder_extractor.errors import FetchError
from terminschleuder_extractor.extractors.llm import LLMExtractor


class FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class FakeChat:
    def __init__(self, response):
        self.completions = FakeCompletions(response)


class FakeLLM:
    def __init__(self, response):
        self.chat = FakeChat(response)


@pytest.fixture
def extractor(settings, sample_source):
    return LLMExtractor(settings)


def _page_html(events=None, with_jsonld=False):
    parts = ["<html><head><title>Events</title>"]
    if with_jsonld:
        parts.append(
            '<script type="application/ld+json">{"@type":"Event","name":"LD"}</script>'
        )
    parts.append("</head><body>")
    parts.append("<nav>menu</nav><footer>foot</footer>")
    if events is None:
        parts.append("<h1>PyGraten</h1><p>2025-09-15 19:00</p>")
    else:
        for e in events:
            parts.append(f"<div><h2>{e['title']}</h2><time>{e['starts_at']}</time></div>")
    parts.append("</body></html>")
    return "".join(parts)


@respx.mock
def test_extract_happy_path(extractor, sample_source, make_tool_response, event_dict):
    respx.get(sample_source.url).mock(return_value=respx.MockResponse(200, text=_page_html()))
    events = [event_dict(), event_dict(title="Second", starts_at="2025-10-01T10:00:00")]
    extractor._llm_client = FakeLLM(make_tool_response(events=events))

    obs = extractor.extract(sample_source)

    assert len(obs) == 2
    assert obs[0].source == 42  # stamped from source
    assert obs[0].title == "PyGraten"
    assert obs[1].title == "Second"
    # url defaulted to source.url when the model omits it
    assert obs[0].url == sample_source.url


@respx.mock
def test_extract_empty(extractor, sample_source, make_tool_response):
    respx.get(sample_source.url).mock(return_value=respx.MockResponse(200, text=_page_html()))
    extractor._llm_client = FakeLLM(make_tool_response(events=[]))
    assert extractor.extract(sample_source) == []


@respx.mock
def test_extract_drops_invalid_keeps_valid(extractor, sample_source, make_tool_response, event_dict):
    respx.get(sample_source.url).mock(return_value=respx.MockResponse(200, text=_page_html()))
    events = [
        event_dict(),  # valid
        {"title": "no start"},  # invalid: missing starts_at
        event_dict(title="Third", starts_at="2025-11-01T10:00:00"),  # valid
    ]
    extractor._llm_client = FakeLLM(make_tool_response(events=events))
    obs = extractor.extract(sample_source)
    assert [o.title for o in obs] == ["PyGraten", "Third"]


@respx.mock
def test_extract_content_json_fallback(extractor, sample_source, event_dict):
    """Model returns events in message content (no tool call) — fallback parses it."""
    respx.get(sample_source.url).mock(return_value=respx.MockResponse(200, text=_page_html()))
    events = [event_dict()]
    content = json.dumps(events)
    extractor._llm_client = FakeLLM(
        type("R", (), {"choices": [{"message": {"content": content, "tool_calls": None}}]})()
    )
    obs = extractor.extract(sample_source)
    assert len(obs) == 1
    assert obs[0].title == "PyGraten"


@respx.mock
def test_extract_fenced_json_fallback(extractor, sample_source, event_dict):
    respx.get(sample_source.url).mock(return_value=respx.MockResponse(200, text=_page_html()))
    content = f"```json\n{json.dumps([event_dict()])}\n```"
    extractor._llm_client = FakeLLM(
        type("R", (), {"choices": [{"message": {"content": content, "tool_calls": None}}]})()
    )
    assert len(extractor.extract(sample_source)) == 1


@respx.mock
def test_extract_no_tool_call_no_json_returns_empty(extractor, sample_source):
    respx.get(sample_source.url).mock(return_value=respx.MockResponse(200, text=_page_html()))
    extractor._llm_client = FakeLLM(
        type("R", (), {"choices": [{"message": {"content": "no events here", "tool_calls": None}}]})()
    )
    assert extractor.extract(sample_source) == []


@respx.mock
def test_fetch_error_on_404(extractor, sample_source):
    respx.get(sample_source.url).mock(return_value=respx.MockResponse(404, text="nope"))
    with pytest.raises(FetchError):
        extractor.extract(sample_source)


@respx.mock
def test_clean_strips_noise_and_collects_jsonld(extractor, sample_source):
    html = _page_html(with_jsonld=True)
    text, jsonld = extractor._clean_html(html)
    assert "menu" not in text  # nav stripped
    assert "foot" not in text  # footer stripped
    assert "PyGraten" in text
    assert len(jsonld) == 1
    assert "Event" in jsonld[0]


@respx.mock
def test_forced_tool_choice_passed(extractor, sample_source, make_tool_response, event_dict):
    respx.get(sample_source.url).mock(return_value=respx.MockResponse(200, text=_page_html()))
    extractor._llm_client = FakeLLM(make_tool_response(events=[event_dict()]))
    extractor.extract(sample_source)
    call = extractor._llm_client.chat.completions.calls[0]
    assert call["tool_choice"] == {"type": "function", "function": {"name": "extract_events"}}
    assert call["tools"][0]["function"]["name"] == "extract_events"
    assert call["temperature"] == 0.0