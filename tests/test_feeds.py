"""Tests for feed discovery + jcal/iCal parsing and the extractor feed fallback.

Covers the pure parsers (jcal/iCal) directly, discovery + time-param filling
with injected fakes, and an end-to-end integration through ``LLMExtractor``
that mirrors the raumzeitlabor.de case (a JS-rendered skeleton whose real events
live in a jcal feed referenced only by an external script).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import respx

from terminschleuder_extractor.extractors.llm import LLMExtractor
from terminschleuder_extractor.feeds import (
    discover_feed_urls,
    fill_time_params,
    filter_upcoming,
    is_past,
    parse_calendar,
)
from terminschleuder_extractor.models import DueSource, OrganizationMini

# --- jcal -----------------------------------------------------------------


JCAL = json.dumps(
    [
        "vcalendar",
        [["version", {}, "text", "2.0"]],
        [
            [
                "vevent",
                [
                    ["summary", {}, "text", "Hacklab Open Night"],
                    ["dtstart", {}, "date-time", "2099-09-03T16:00:00Z"],
                    ["dtend", {}, "date-time", "2099-09-03T22:00:00Z"],
                    ["location", {}, "text", "RaumZeitLabor"],
                    ["description", {}, "text", "Weekly open night"],
                    ["uid", {}, "text", "evt-1@rzl.de"],
                ],
                [],
            ],
            [
                "vevent",
                [
                    ["summary", {}, "text", "All-Day Workshop"],
                    ["dtstart", {}, "date", "2099-09-04"],
                    ["dtend", {}, "date", "2099-09-05"],
                    ["location", {}, "text", "RZL"],
                ],
                [],
            ],
            # no summary -> dropped
            ["vevent", [["dtstart", {}, "date", "2099-09-06"]], []],
            # no dtstart -> dropped
            ["vevent", [["summary", {}, "text", "No Date"]], []],
        ],
    ]
)


def test_parse_jcal_utc_and_date_only():
    events = parse_calendar(JCAL, "application/calendar+json")
    assert events is not None
    titles = [e["title"] for e in events]
    assert titles == ["Hacklab Open Night", "All-Day Workshop"]

    e0 = events[0]
    assert e0["starts_at"] == datetime(2099, 9, 3, 16, 0, 0, tzinfo=timezone.utc)
    assert e0["ends_at"] == datetime(2099, 9, 3, 22, 0, 0, tzinfo=timezone.utc)
    assert e0["venue_name"] == "RaumZeitLabor"
    assert e0["description"] == "Weekly open night"
    assert e0["url"] is None  # uid is NOT used as a url
    assert e0["uid"] == "evt-1@rzl.de"  # uid carried as the event identity

    # date-only -> midnight naive datetime
    e1 = events[1]
    assert e1["starts_at"] == datetime(2099, 9, 4, 0, 0)
    assert e1["starts_at"].tzinfo is None
    assert e1["uid"] is None  # no uid on this vevent


def test_parse_jcal_detected_by_content_without_content_type():
    # No content-type hint, but the payload starts with ["vcalendar"
    events = parse_calendar(JCAL, "")
    assert events is not None
    assert len(events) == 2


def test_parse_calendar_rss_is_none():
    # RSS/Atom/JSON/HTML are not parsed directly -> None signals LLM fallback.
    assert parse_calendar("<rss><channel></channel></rss>", "application/rss+xml") is None
    assert parse_calendar('{"events": []}', "application/json") is None
    assert parse_calendar("<html><body>nope</body></html>", "text/html") is None


# --- iCal -----------------------------------------------------------------


ICAL = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:evt-1@rzl.de\r\n"
    "SUMMARY:Hacklab Open Night\r\n"
    "DTSTART:20990903T160000Z\r\n"
    "DTEND:20990903T220000Z\r\n"
    "LOCATION:RaumZeitLabor\r\n"
    "DESCRIPTION:abc\r\n"
    " def\r\n"  # folded continuation -> "abcdef"
    "URL:https://rzl.de/events/1\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "SUMMARY:All-Day Workshop\r\n"
    "DTSTART;VALUE=DATE:20990904\r\n"
    "DTEND;VALUE=DATE:20990905\r\n"
    "LOCATION:RZL\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"  # no summary -> dropped
    "DTSTART:20990910T100000Z\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_parse_ical_unfolding_utc_date_and_url():
    events = parse_calendar(ICAL, "text/calendar")
    assert events is not None
    assert [e["title"] for e in events] == ["Hacklab Open Night", "All-Day Workshop"]

    e0 = events[0]
    assert e0["starts_at"] == datetime(2099, 9, 3, 16, 0, 0, tzinfo=timezone.utc)
    assert e0["ends_at"] == datetime(2099, 9, 3, 22, 0, 0, tzinfo=timezone.utc)
    assert e0["venue_name"] == "RaumZeitLabor"
    # folded line unfolded: "abc" + "def"
    assert e0["description"] == "abcdef"
    assert e0["url"] == "https://rzl.de/events/1"
    assert e0["uid"] == "evt-1@rzl.de"

    # VALUE=DATE date-only -> midnight naive
    e1 = events[1]
    assert e1["starts_at"] == datetime(2099, 9, 4, 0, 0)
    assert e1["starts_at"].tzinfo is None


def test_parse_ical_tzid_kept_naive():
    ical = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
        "SUMMARY:TZID Event\r\n"
        "DTSTART;TZID=Europe/Berlin:20990903T190000\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events = parse_calendar(ical, "text/calendar")
    assert events is not None
    # TZID can't be resolved without tzdata; local time is kept naive (not guessed).
    assert events[0]["starts_at"] == datetime(2099, 9, 3, 19, 0, 0)
    assert events[0]["starts_at"].tzinfo is None


# --- filter_upcoming / is_past -------------------------------------------


def test_filter_upcoming_drops_past(fixed_now):
    past = datetime(2024, 1, 1, tzinfo=timezone.utc)
    future = datetime(2025, 6, 1, tzinfo=timezone.utc)
    events = [
        {"title": "Past", "starts_at": past},
        {"title": "Future", "starts_at": future},
    ]
    out = filter_upcoming(events, fixed_now)
    assert [e["title"] for e in out] == ["Future"]


def test_is_past_treats_naive_as_utc(fixed_now):
    # naive datetime compared against tz-aware now is treated as UTC, not an error
    assert is_past(datetime(2024, 1, 1), fixed_now) is True
    assert is_past(datetime(2025, 6, 1), fixed_now) is False


# --- fill_time_params -----------------------------------------------------


def test_fill_time_params_empty_start_end_as_unix(fixed_now):
    url = "https://rzl.de/events/ical?accept=jcal&expand=true&start=&end="
    filled = fill_time_params(url, fixed_now, timedelta(days=180))
    assert f"start={int(fixed_now.timestamp())}" in filled
    assert f"end={int((fixed_now + timedelta(days=180)).timestamp())}" in filled


def test_fill_time_params_adds_missing_end(fixed_now):
    # sabre/dav requires both start and end for expand=true.
    url = "https://rzl.de/events/ical?expand=true&start="
    filled = fill_time_params(url, fixed_now, timedelta(days=30))
    assert f"start={int(fixed_now.timestamp())}" in filled
    assert f"end={int((fixed_now + timedelta(days=30)).timestamp())}" in filled


def test_fill_time_params_preserves_set_values(fixed_now):
    url = "https://rzl.de/cal?start=100&end=200"
    filled = fill_time_params(url, fixed_now, timedelta(days=30))
    assert "start=100" in filled
    assert "end=200" in filled


def test_fill_time_params_timemin_timemax_rfc3339(fixed_now):
    from urllib.parse import parse_qs, urlparse

    url = "https://api.example/events?timeMin=&timeMax="
    filled = fill_time_params(url, fixed_now, timedelta(days=7))
    qs = parse_qs(urlparse(filled).query)
    # both filled (non-empty), timeMin == now, timeMax == now+window
    assert qs["timeMin"] == [fixed_now.isoformat()]
    assert qs["timeMax"] == [(fixed_now + timedelta(days=7)).isoformat()]


# --- discover_feed_urls ---------------------------------------------------


def _noop_fetch(_url):
    return None, None


def test_discover_feed_urls_link_tag():
    html = (
        '<html><head>'
        '<link rel="alternate" type="text/calendar" href="/events.ics">'
        '<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
        '<link rel="stylesheet" href="/style.css">'
        '</head><body></body></html>'
    )
    urls = discover_feed_urls(html, "https://example.com/", _noop_fetch)
    assert "https://example.com/events.ics" in urls
    assert "https://example.com/feed.xml" in urls
    assert all("style.css" not in u for u in urls)


def test_discover_feed_urls_inline_script():
    html = (
        '<html><body>'
        '<script>const feed = "/events/ical?accept=jcal&start=&end=";</script>'
        '</body></html>'
    )
    urls = discover_feed_urls(html, "https://rzl.de/events/", _noop_fetch)
    assert "https://rzl.de/events/ical?accept=jcal&start=&end=" in urls


def test_discover_feed_urls_external_js_scan():
    html = '<html><body><script src="/static/events.js"></script></body></html>'

    def fetch_text(url):
        if url == "https://rzl.de/static/events.js":
            return (
                'const u="/events/ical?accept=jcal&expand=true&start="+t+"&end="+e;',
                "text/javascript",
            )
        return None, None

    urls = discover_feed_urls(html, "https://rzl.de/events/", fetch_text, max_scripts=3)
    assert "https://rzl.de/events/ical?accept=jcal&expand=true&start=" in urls


def test_discover_feed_urls_dedups_and_drops_source_page():
    # same feed referenced twice, plus the source page itself -> deduped/dropped
    html = (
        '<html><head><link rel="alternate" type="text/calendar" href="/events.ics"></head>'
        '<body><script>fetch("/events.ics")</script></body></html>'
    )
    urls = discover_feed_urls(html, "https://example.com/events", _noop_fetch)
    assert urls.count("https://example.com/events.ics") == 1
    assert "https://example.com/events" not in urls  # source page dropped


# --- integration through LLMExtractor (raumzeitlabor-style) ---------------


def _rzl_source() -> DueSource:
    return DueSource(
        id=22,
        organization=OrganizationMini(id=9, name="RZL", slug="rzl"),
        url="https://raumzeitlabor.de/events/index.html",
        platform="llm",
        fetch_interval_minutes=60,
        last_fetched_at=None,
        next_due_at=None,
    )


@respx.mock
def test_extract_feed_fallback_jcal(settings, make_tool_response, event_dict):
    """A JS-rendered skeleton: listing finds nothing, the feed is discovered via
    an external script, filled, and parsed directly into observations."""
    src = _rzl_source()
    skeleton = (
        '<html><body><div id="calendar">lade…</div>'
        '<script src="/static/events.js"></script></body></html>'
    )
    events_js = (
        'const t=Math.floor(Date.now()/1000);'
        'fetch("/events/ical?accept=jcal&expand=true&start="+t+"&end="+(t+15552000));'
    )

    respx.get(src.url).mock(return_value=respx.MockResponse(200, text=skeleton))
    respx.get("https://raumzeitlabor.de/static/events.js").mock(
        return_value=respx.MockResponse(200, text=events_js, headers={"content-type": "text/javascript"})
    )
    # The filled feed URL has computed start/end; match the prefix.
    respx.route(method="GET", url__startswith="https://raumzeitlabor.de/events/ical").mock(
        return_value=respx.MockResponse(200, text=JCAL, headers={"content-type": "application/calendar+json"})
    )

    extractor = LLMExtractor(settings)
    # listing pass returns nothing (skeleton page).
    extractor._llm_client = _SingleResponseLLM(make_tool_response(events=[]))

    obs = extractor.extract(src)

    titles = {o.title for o in obs}
    assert "Hacklab Open Night" in titles
    assert "All-Day Workshop" in titles
    # only the listing LLM call happened (feed parsed directly, no feed LLM call)
    assert len(extractor._llm_client.chat.completions.calls) == 1


@respx.mock
def test_extract_feed_merged_with_listing(settings, make_tool_response, event_dict):
    """Listing already has one event; the feed supplies a second distinct one and
    is merged in (no spurious duplicate of the listing event)."""
    src = _rzl_source()
    # a <link> calendar hint with empty start/end params (filled at fetch time)
    skeleton = (
        '<html><head>'
        '<link rel="alternate" type="text/calendar" href="/events/ical?expand=true&start=&end=">'
        '</head><body><div id="cal">loading</div></body></html>'
    )
    respx.get(src.url).mock(return_value=respx.MockResponse(200, text=skeleton))
    respx.route(method="GET", url__startswith="https://raumzeitlabor.de/events/ical").mock(
        return_value=respx.MockResponse(200, text=JCAL, headers={"content-type": "application/calendar+json"})
    )

    extractor = LLMExtractor(settings)
    # listing returns one event that also appears in the feed (same title+date) -> deduped
    extractor._llm_client = _SingleResponseLLM(
        make_tool_response(
            events=[event_dict(title="Hacklab Open Night", starts_at="2099-09-03T16:00:00")]
        )
    )

    obs = extractor.extract(src)

    titles = [o.title for o in obs]
    # the duplicate is merged away; the distinct workshop is added
    assert titles.count("Hacklab Open Night") == 1
    assert "All-Day Workshop" in titles


@respx.mock
def test_extract_feed_disabled(settings, make_tool_response, event_dict, settings_factory):
    """discover_feeds=False suppresses all feed fetching."""
    src = _rzl_source()
    skeleton = (
        '<html><body><div id="cal">loading</div>'
        '<script src="/static/events.js"></script></body></html>'
    )
    respx.get(src.url).mock(return_value=respx.MockResponse(200, text=skeleton))
    # No route for the JS or the feed: assert_all_mocked (default True) fails if
    # any feed fetch is attempted.
    extractor = LLMExtractor(settings_factory(discover_feeds=False))
    extractor._llm_client = _SingleResponseLLM(make_tool_response(events=[event_dict()]))

    obs = extractor.extract(src)
    assert len(obs) == 1


# --- test helpers ---------------------------------------------------------


class _SingleResponseLLM:
    """Minimal fake OpenAI client returning one fixed response (listing pass)."""

    def __init__(self, response):
        self.chat = type(
            "Chat",
            (),
            {
                "completions": type(
                    "Completions",
                    (),
                    {
                        "calls": [],
                        "create": lambda self_, **kw: (
                            self_.calls.append(kw) or response
                        ),
                    },
                )()
            },
        )()