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


class FakeCompletionsSequence:
    """Returns a queued sequence of responses, one per ``create()`` call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeLLMSequence:
    """Fake LLM client returning a different response per call (listing + details)."""

    def __init__(self, responses):
        self.chat = type("FakeChat", (), {"completions": FakeCompletionsSequence(responses)})()


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
    # url is the event's own page if the model returns one; when omitted it is
    # None (we never fake it to the source URL — provenance is in `source`).
    assert obs[0].url is None


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


# --- link preservation in cleaning ---


def test_clean_preserves_and_absolutizes_hrefs(extractor, sample_source):
    html = (
        '<html><body><main>'
        '<h2><a href="/events/e1">Event One</a></h2>'
        '<a href="https://other.example/x">Other</a>'
        '<a href="#frag">skip</a>'
        '</main></body></html>'
    )
    text, _ = extractor._clean_html(html, source_url="https://example.com/events")
    # relative href resolved against the source URL
    assert "Event One [https://example.com/events/e1]" in text
    # absolute href kept as-is (fragment stripped)
    assert "Other [https://other.example/x]" in text
    # fragment-only link contributes its label but no [url]
    assert "[#frag]" not in text


def test_clean_without_preserve_links_drops_hrefs(extractor, sample_source):
    html = '<html><body><a href="/events/e1">Event One</a></body></html>'
    text, _ = extractor._clean_html(html, source_url="https://example.com/events", preserve_links=False)
    assert "Event One" in text
    assert "[" not in text


# --- detail-link following ---


@respx.mock
def test_extract_follows_detail_links_and_merges(extractor, sample_source, make_tool_response, event_dict):
    detail_url = "https://example.com/events/e1"
    respx.get(sample_source.url).mock(return_value=respx.MockResponse(200, text=_page_html()))
    respx.get(detail_url).mock(return_value=respx.MockResponse(200, text="<html><body><h1>E1</h1></body></html>"))

    listing = [event_dict(title="E1", starts_at="2026-08-27T00:00:00", url=detail_url)]
    detail = [{
        "title": "E1",
        "starts_at": "2026-08-27T09:00:00",
        "venue_name": "Congress Centrum",
        "venue_city": "Bremen",
        "description": "A big conference",
    }]
    extractor._llm_client = FakeLLMSequence([
        make_tool_response(events=listing),
        make_tool_response(events=detail),
    ])

    obs = extractor.extract(sample_source)

    assert len(obs) == 1
    assert obs[0].url == detail_url
    # detail time-of-day overrode the listing date-only value
    assert obs[0].starts_at.isoformat() == "2026-08-27T09:00:00"
    assert obs[0].venue_city == "Bremen"
    assert obs[0].venue_name == "Congress Centrum"
    assert obs[0].description == "A big conference"
    # one listing call + one detail call
    assert len(extractor._llm_client.chat.completions.calls) == 2


@respx.mock
def test_detail_fetch_failure_keeps_listing_event(extractor, sample_source, make_tool_response, event_dict):
    detail_url = "https://example.com/events/missing"
    respx.get(sample_source.url).mock(return_value=respx.MockResponse(200, text=_page_html()))
    respx.get(detail_url).mock(return_value=respx.MockResponse(404, text="nope"))

    listing = [event_dict(title="E1", starts_at="2026-08-27T00:00:00", url=detail_url)]
    extractor._llm_client = FakeLLMSequence([make_tool_response(events=listing)])

    obs = extractor.extract(sample_source)

    assert len(obs) == 1
    assert obs[0].title == "E1"
    assert obs[0].url == detail_url  # detail URL still recorded
    assert obs[0].starts_at.isoformat() == "2026-08-27T00:00:00"  # listing value kept
    # only the listing LLM call happened (detail fetch failed before the LLM)
    assert len(extractor._llm_client.chat.completions.calls) == 1


@respx.mock
def test_source_url_not_refetched_as_detail(extractor, sample_source, make_tool_response, event_dict):
    respx.get(sample_source.url).mock(return_value=respx.MockResponse(200, text=_page_html()))
    # model wrongly attributes the source URL as the event's own page
    listing = [event_dict(title="E1", starts_at="2026-08-27T00:00:00", url=sample_source.url)]
    extractor._llm_client = FakeLLMSequence([make_tool_response(events=listing)])

    obs = extractor.extract(sample_source)

    assert len(obs) == 1
    assert obs[0].url is None  # source URL is not a distinct event page -> dropped
    assert len(extractor._llm_client.chat.completions.calls) == 1  # no detail call


@respx.mock
def test_detail_pages_capped_by_settings(extractor, sample_source, settings_factory, make_tool_response, event_dict):
    # Rebuild extractor with a cap of 1 detail page.
    capped = LLMExtractor(settings_factory(max_detail_pages_per_source=1))
    detail_urls = [f"https://example.com/events/e{i}" for i in range(3)]
    respx.get(sample_source.url).mock(return_value=respx.MockResponse(200, text=_page_html()))
    for u in detail_urls:
        respx.get(u).mock(return_value=respx.MockResponse(200, text="<html><body><h1>x</h1></body></html>"))

    listing = [
        event_dict(title=f"E{i}", starts_at="2026-08-27T00:00:00", url=detail_urls[i])
        for i in range(3)
    ]
    # Only one detail response is ever consumed (the cap stops further fetches).
    capped._llm_client = FakeLLMSequence([
        make_tool_response(events=listing),
        make_tool_response(events=[{"title": "E0", "starts_at": "2026-08-27T09:00:00", "venue_city": "Bremen"}]),
    ])

    obs = capped.extract(sample_source)

    assert len(obs) == 3
    # exactly one detail page fetched -> one listing + one detail LLM call
    assert len(capped._llm_client.chat.completions.calls) == 2
    # the capped event keeps its listing value but still records its detail url
    assert obs[0].venue_city == "Bremen"  # enriched (the one followed)
    assert obs[1].venue_city is None     # not followed (cap)
    assert obs[1].url == detail_urls[1]  # url still recorded
    assert obs[2].url == detail_urls[2]


@respx.mock
def test_detail_following_disabled_when_cap_zero(extractor, sample_source, settings_factory, make_tool_response, event_dict):
    no_detail = LLMExtractor(settings_factory(max_detail_pages_per_source=0))
    detail_url = "https://example.com/events/e1"
    respx.get(sample_source.url).mock(return_value=respx.MockResponse(200, text=_page_html()))
    # If detail following ran, this unmatched route would raise -> assert_all_mocked
    # is True by default, so the test fails if a detail fetch is attempted.
    listing = [event_dict(title="E1", starts_at="2026-08-27T00:00:00", url=detail_url)]
    no_detail._llm_client = FakeLLMSequence([make_tool_response(events=listing)])

    obs = no_detail.extract(sample_source)

    assert len(obs) == 1
    assert obs[0].url == detail_url  # url kept, but not fetched
    assert len(no_detail._llm_client.chat.completions.calls) == 1  # listing only