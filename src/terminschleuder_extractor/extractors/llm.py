"""The LLM-based extractor.

Fetches a source page with httpx, cleans the HTML with BeautifulSoup, and asks
an **OpenAI-compatible** chat-completion endpoint to return events as
structured JSON via a forced function tool call (``extract_events``). The model
output is **untrusted**: every item is validated with pydantic and bad items
are dropped with a WARNING — one malformed event never crashes a whole source.

Two-phase extraction (general, site-agnostic):
  1. *Listing pass* — extract every event from the source page itself. The
     cleaned text inlines anchor hrefs as ``text [URL]`` so the model can
     return each event's own detail-page URL in its ``url`` field.
  2. *Detail pass* — for events whose ``url`` is a distinct page, fetch that
     page and run a focused single-event extraction to recover the time of day,
     full venue/address, coordinates, and description that usually only appear
     on the detail page. Detail fields (non-empty) override the listing values;
     a failed detail fetch leaves the listing event intact. Bounded by
     ``max_detail_pages_per_source`` (0 disables it).

Provider portability: we point the ``openai`` SDK at a configurable
``base_url`` (local Ollama by default; any OpenAI-compatible endpoint works).
We do **not** set ``strict: True`` on the tool schema (many compatible backends
ignore it) and we keep a content-JSON fallback for models that emit the events
in the message body instead of via a tool call (common with small Ollama
models). No agentic loop — forced calls, parse, done.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha1
from typing import Any
from urllib.parse import urljoin, urldefrag

import httpx
from bs4 import BeautifulSoup

from ..config import Settings
from ..errors import FetchError, LlmError
from ..feeds import (
    discover_feed_urls,
    fill_time_params,
    filter_upcoming,
    parse_calendar,
)
from ..logging_setup import get_logger
from ..models import AttendanceMode, DueSource, EventType, ObservationSubmit
from .base import Extractor, register

# Tags that carry no event content and only bloat the prompt.
_NOISE_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "footer",
    "header",
    "aside",
    "form",
    "svg",
)

# The function tool the model must call. We deliberately omit ``strict: True``
# (not universally honored by OpenAI-compatible servers) and rely on pydantic
# validation downstream. The schema describes one event object; the tool
# returns ``{"events": [event, ...]}``.
_EVENT_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Event title (required)."},
        "starts_at": {
            "type": "string",
            "description": (
                "Start datetime in ISO-8601, INCLUDING the time of day when "
                "shown, e.g. 2025-09-15T19:00:00. If only a date is known, "
                "use 2025-09-15T00:00:00. Preserve timezone offset if shown."
            ),
        },
        "ends_at": {
            "type": ["string", "null"],
            "description": (
                "End datetime in ISO-8601 with time of day if known. For a "
                "date range 'A - B', starts_at=A and ends_at=B."
            ),
        },
        "description": {
            "type": ["string", "null"],
            "description": "Short summary/description if present on the page.",
        },
        "url": {
            "type": ["string", "null"],
            "description": (
                "This event's own detail-page URL when the page links to one "
                "(shown in the text as `link text [URL]`). Use that [URL]. "
                "Do NOT use the source page URL. Omit if the event has no "
                "distinct page."
            ),
        },
        "platform": {"type": ["string", "null"], "description": "Observed platform/site name."},
        "attendance_mode": {
            "type": "string",
            "enum": [e.value for e in AttendanceMode],
            "description": "physical | online | hybrid. Infer from the page; default physical.",
        },
        "event_type": {
            "type": "string",
            "enum": [e.value for e in EventType],
            "description": (
                "meetup | conference | workshop | social | other. Map the "
                "page's category/label to the closest value (e.g. "
                "conference/Konferenz -> conference, workshop -> workshop, "
                "a casual gathering -> social or meetup). Use `other` only "
                "if nothing fits."
            ),
        },
        "venue_name": {
            "type": ["string", "null"],
            "description": "Venue/building/organizer location name if shown.",
        },
        "venue_address": {
            "type": ["string", "null"],
            "description": "Street + number + postal code if shown.",
        },
        "venue_city": {"type": ["string", "null"], "description": "City/town if shown."},
        "latitude": {"type": ["number", "null"], "description": "Decimal degrees if coordinates appear."},
        "longitude": {"type": ["number", "null"], "description": "Decimal degrees if coordinates appear."},
    },
    "required": ["title", "starts_at"],
}

EXTRACT_EVENTS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "extract_events",
        "description": (
            "Extract events from the page as a list of event objects. On a "
            "listing page, return every event shown; on a single-event detail "
            "page, return that one event. Call this tool exactly once. If there "
            "are no events, return an empty list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "events": {
                    "type": "array",
                    "items": _EVENT_OBJECT_SCHEMA,
                    "description": "All events found on the page (may be empty).",
                }
            },
            "required": ["events"],
        },
    },
}

# Listing pass: the source page may be a single event or a list of many.
_LISTING_SYSTEM_PROMPT = """\
You are an event extraction engine for the terminschleuder event directory.
You are given the cleaned text of a web page (plus any JSON-LD blocks) and the
page URL. The page is either a single event's page or a listing of many events.
Extract every upcoming event you can find and return them via the `extract_events`
tool.

Links: anchor links appear as `link text [URL]`, where [URL] is the link's
absolute destination. When an event has its own detail page linked from this
page, set that event's `url` to that detail page's [URL]. Do NOT set `url` to
the source page URL itself. If an event has no distinct page, omit `url`.

Rules:
- Return ONLY events that have at least a title and a start datetime. If you
  cannot confidently determine either, omit the event.
- `starts_at`/`ends_at` are ISO-8601. Capture the TIME OF DAY when shown and
  combine date + time into one timestamp, e.g. "2025-09-15T19:00:00". If only a
  date is shown, use that date at midnight, e.g. "2025-09-15T00:00:00". Preserve
  the timezone/offset as shown; if none is shown, use local naive time.
- Recognize common date formats on the page (e.g. 15.09.2025, 09/15/2025,
  15 September 2025, 2025-09-15) and normalize to ISO-8601. For a date range
  "A - B", set starts_at=A and ends_at=B.
- `venue_name`: the venue/building/organizer location name if shown.
- `venue_address`: street + number + postal code if shown.
- `venue_city`: the city/town if shown.
- `latitude`/`longitude`: decimal degrees if coordinates appear; otherwise omit.
  Send them as separate numbers.
- `description`: a short summary if the page has one; otherwise omit.
- `attendance_mode`: physical | online | hybrid. Infer from clues (a physical
  venue, or "Online"/"Hybrid"/"Zoom"/"remote"); default physical.
- `event_type`: meetup | conference | workshop | social | other. Map the page's
  category/label to the closest value (e.g. conference/Konferenz -> conference,
  workshop -> workshop, a casual/regular gathering -> social or meetup). Use
  `other` only if nothing fits.
- `platform`: the site/platform name if known; otherwise omit.
- If the page lists no events at all, return an empty `events` array.
- Do not invent events. Do not include past events. Do not include duplicates.
"""

# Detail pass: a single event's own page. Focus on fields that usually only
# appear here (exact time, full venue, coordinates, description).
_DETAIL_SYSTEM_PROMPT = """\
You are an event extraction engine for the terminschleuder event directory.
You are given the cleaned text of a SINGLE event's detail page (plus any
JSON-LD blocks) and its URL. Extract THAT ONE event with its full details and
return it via the `extract_events` tool (a list with one event, or an empty list
if this page is not an event).

Focus on the fields that usually only appear on the detail page:
- Exact `starts_at`/`ends_at` INCLUDING the time of day, as ISO-8601 (e.g.
  "2025-09-15T09:00:00"). Combine date + time. For a range, starts_at is the
  start and ends_at is the end. Preserve the timezone/offset as shown; if none
  is shown, use local naive time. Recognize common date/time formats (e.g.
  15.09.2026, 09:00 - 21.08.2026, 18:00) and normalize to ISO-8601.
- `venue_name`, `venue_address` (street + number + postal code), `venue_city`.
- `latitude`/`longitude` (decimal degrees) if coordinates appear.
- `description`: a short summary from the page.
- `attendance_mode` (physical | online | hybrid) and `event_type`
  (meetup | conference | workshop | social | other), mapped from the page's
  category/label as in the listing pass.
- `title` if shown on this page.

Rules:
- Return AT MOST the one event this page is about. Do not pull in unrelated or
  "related events" listed elsewhere on the page.
- If the page is not an event detail page, return an empty `events` array.
- Do not invent information not present on the page.
"""


@register("llm")
class LLMExtractor(Extractor):
    """Fetch + clean a source page, then extract events via an LLM tool call."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.Client | None = None,
        llm_client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._log = get_logger("extractor.llm")
        self._http_client = http_client
        # The OpenAI-compatible client. Lazy-constructed so --self-test works
        # without a reachable endpoint.
        self._llm_client = llm_client

    # --- HTTP fetch ---
    def _fetch_html(self, url: str) -> str:
        client = self._http_client or httpx.Client(
            headers={"User-Agent": self._settings.user_agent},
            timeout=self._settings.http_timeout_seconds,
            follow_redirects=True,
        )
        owns = self._http_client is None
        try:
            resp = client.get(url)
        except httpx.HTTPError as exc:
            raise FetchError(f"fetch failed for {url}: {exc}") from exc
        finally:
            if owns:
                client.close()
        if resp.status_code >= 400:
            raise FetchError(f"fetch {url} returned HTTP {resp.status_code}")
        return resp.text

    def _fetch_text(self, url: str) -> tuple[str | None, str | None]:
        """Soft fetch used by feed discovery/parse: returns (text, content_type)
        or (None, None) on any failure. Never raises — a missing feed is fine."""
        client = self._http_client or httpx.Client(
            headers={"User-Agent": self._settings.user_agent},
            timeout=self._settings.http_timeout_seconds,
            follow_redirects=True,
        )
        owns = self._http_client is None
        try:
            resp = client.get(url)
        except httpx.HTTPError:
            return None, None
        finally:
            if owns:
                client.close()
        if resp.status_code >= 400:
            return None, None
        return resp.text, resp.headers.get("content-type", "")

    # --- HTML cleaning ---
    def _clean_html(
        self, html: str, *, source_url: str = "", preserve_links: bool = True
    ) -> tuple[str, list[str]]:
        """Return (visible_text, jsonld_blocks). Strips noise tags + trims length.

        When ``preserve_links`` is set, anchor hrefs are resolved against
        ``source_url`` and inlined into the text as ``link text [URL]`` so the
        model can see and return per-event detail URLs (``get_text`` otherwise
        drops hrefs entirely). Noise tags (nav/footer/script/...) are removed
        first, so only content-area links are kept.
        """
        soup = BeautifulSoup(html, "html.parser")

        # Collect JSON-LD blocks *before* stripping scripts (JSON-LD lives in
        # <script type="application/ld+json">, which the noise-strip would remove).
        jsonld_blocks: list[str] = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            if script.string:
                jsonld_blocks.append(script.string.strip())

        for tag in soup(list(_NOISE_TAGS)):
            tag.decompose()

        if preserve_links:
            # Inline absolute hrefs as `text [URL]`. Mutating the anchor's
            # content (replacing children with a single string) is enough — we
            # only read the tree via get_text afterwards.
            for a in soup.find_all("a", href=True):
                label = a.get_text(" ", strip=True)
                if not label:
                    continue
                href = a["href"].strip()
                if not href or href.startswith("#"):
                    continue
                absolute = urljoin(source_url, href) if source_url else href
                # Drop fragments; they don't identify a distinct page.
                absolute, _ = urldefrag(absolute)
                a.string = f"{label} [{absolute}]"

        text = soup.get_text(separator="\n", strip=True)
        # Collapse blank lines for a denser prompt.
        lines = [line for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)
        max_chars = self._settings.max_page_chars
        if len(text) > max_chars:
            text = text[:max_chars]
            text += "\n…[truncated]"
        return text, jsonld_blocks

    # --- LLM call ---
    def _llm(self) -> Any:
        if self._llm_client is None:
            from openai import OpenAI

            self._llm_client = OpenAI(
                base_url=self._settings.llm_base_url,
                api_key=self._settings.require_llm_api_key(),
            )
        return self._llm_client

    def _call_model(self, system: str, user: str) -> str:
        """Call the model with a forced extract_events tool; return raw content.

        Returns the message content (for the fallback path). The tool call, if
        any, is read by the caller via the returned message object — but since
        the openai SDK returns a Pydantic-ish object, we expose it via a small
        helper. Here we keep it simple: we make the call and stash the response
        on ``self._last_response`` for ``_parse_response`` to inspect.
        """
        client = self._llm()
        response = client.chat.completions.create(
            model=self._settings.llm_model,
            max_tokens=self._settings.llm_max_tokens,
            temperature=self._settings.llm_temperature,
            tools=[EXTRACT_EVENTS_TOOL],
            tool_choice={"type": "function", "function": {"name": "extract_events"}},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        self._last_response = response
        # Return content for the fallback path (may be None).
        try:
            return response.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            return ""

    def _extract_events_list(self) -> list[dict[str, Any]]:
        """Pull the list of event dicts out of the last LLM response.

        Prefer the forced tool call (``tool_calls[0].function.arguments``),
        which is a JSON string like ``{"events": [...]}`` or ``[...]``. If the
        model returned no tool call, fall back to parsing a JSON array out of
        the message content (some local models do this).
        """
        response = getattr(self, "_last_response", None)
        if response is None:
            raise LlmError("no LLM response available")

        # Tool-call path.
        tool_calls = _safe_get(response, "choices", 0, "message", "tool_calls")
        if tool_calls:
            args = _safe_get(tool_calls, 0, "function", "arguments")
            if isinstance(args, str):
                return _coerce_events(json.loads(args))
            if isinstance(args, dict):
                return _coerce_events(args)

        # Content fallback: find the first JSON array in the message content.
        content = _safe_get(response, "choices", 0, "message", "content") or ""
        return _parse_json_array_from_content(content)

    # --- Extractor interface ---
    def extract(self, source: DueSource) -> list[ObservationSubmit]:
        log = self._log.bind(source_id=source.id, url=source.url)
        log.debug("fetching_source", platform=source.platform)
        html = self._fetch_html(source.url)
        text, jsonld = self._clean_html(html, source_url=source.url, preserve_links=True)
        log.debug("page_cleaned", chars=len(text), jsonld_blocks=len(jsonld))

        # Phase 1 — listing pass: extract events from the source page itself.
        event_dicts = self._run_listing(source.url, source.platform, text, jsonld)
        log.info(
            "llm_extracted",
            phase="listing",
            raw_count=len(event_dicts),
            model=self._settings.llm_model,
        )

        # Feed fallback — JS-rendered pages may have no events in the static
        # HTML. Discover the site's event-data feed (calendar link tags + a
        # bounded scan of scripts), parse it directly (jcal/iCal) or via the
        # LLM (RSS/JSON/HTML), and merge the events with the listing results.
        # Always attempted (per design choice): merge with listing output and
        # accept the dedup risk — dedup is by title + start date.
        if self._settings.discover_feeds:
            feed_events = self._discover_and_extract_feeds(html, source, log)
            if feed_events:
                before = len(event_dicts)
                event_dicts = self._merge_events(event_dicts, feed_events)
                log.info(
                    "feed_merged",
                    listing=before,
                    feed=len(feed_events),
                    merged=len(event_dicts),
                )

        # Phase 2 — detail pass: follow per-event detail links to recover the
        # time of day, full venue/address, coordinates, and description that
        # usually only appear on each event's own page. No-op when the source
        # page is itself a single event (no distinct detail URLs).
        event_dicts = self._enrich_with_details(event_dicts, source, log)

        # Compute a stable backend ``event_key`` per event and de-duplicate by it
        # (keeping the richer record) so the backend's per-run unique constraint
        # is never hit. This is the identity the backend reconciles run-over-run
        # (uid for feed events, url for detail events, ``t:<hash>`` fallback).
        event_dicts = self._dedup_by_event_key(event_dicts, source.id, log)

        # Validate + build observations. We do NOT stamp `url` with the source
        # URL: url is the event's own page (if any) or absent. Provenance is
        # already carried by the `source` field.
        observations: list[ObservationSubmit] = []
        for idx, item in enumerate(event_dicts):
            if not isinstance(item, dict):
                log.warning("dropping_non_object_event", index=idx)
                continue
            item = dict(item)
            item.setdefault("source", source.id)
            item.setdefault("platform", source.platform or None)
            # ``uid`` is a feed-only intermediate identity consumed by
            # ``_backend_event_key``; it is not a backend field (ObservationSubmit
            # is extra-forbid), so drop it now that ``event_key`` is stamped.
            item.pop("uid", None)
            try:
                observations.append(ObservationSubmit.model_validate(item))
            except Exception as exc:  # pydantic.ValidationError or similar
                log.warning(
                    "dropping_invalid_event",
                    index=idx,
                    title=item.get("title"),
                    error=str(exc),
                )
        log.info("events_validated", count=len(observations))
        return observations

    def _run_listing(
        self, url: str, platform: str | None, text: str, jsonld: list[str]
    ) -> list[dict[str, Any]]:
        """Listing pass: call the model on a page's text and return event dicts."""
        user_message = self._build_user_message(url, platform, text, jsonld)
        self._call_model(_LISTING_SYSTEM_PROMPT, user_message)
        return self._extract_events_list()

    # --- feed fallback ---
    def _discover_and_extract_feeds(
        self, html: str, source: DueSource, log: Any
    ) -> list[dict[str, Any]]:
        """Discover the source's event-data feed(s) and parse events from it.

        General/site-agnostic. Calendar feeds (jcal/iCal) are parsed directly;
        anything else (RSS/JSON/HTML) is handed to the LLM listing pass. Events
        are de-duplicated against the listing output by the caller. Soft-fails:
        a missing or unparseable feed yields [].
        """
        feed_urls = discover_feed_urls(
            html,
            source.url,
            self._fetch_text,
            max_scripts=self._settings.max_feed_discovery_scripts,
        )
        if not feed_urls:
            return []
        log.debug("feed_candidates", urls=feed_urls)

        now = datetime.now(timezone.utc)
        window = timedelta(days=self._settings.feed_window_days)
        max_events = self._settings.max_feed_events
        # Non-calendar feeds (RSS/Atom/JSON/HTML) deferred to the LLM pass below.
        non_calendar: list[tuple[str, str, str]] = []  # (filled_url, content, ctype)

        # Pass 1 — direct-parse calendar feeds (jcal/iCal). These are lossless
        # (no model in the loop) and therefore preferred over RSS/HTML, so we
        # return the first one that yields upcoming events.
        for feed_url in feed_urls:
            filled = fill_time_params(feed_url, now, window)
            content, ctype = self._fetch_text(filled)
            if content is None:
                continue
            parsed = parse_calendar(content, ctype or "")
            if parsed is None:
                non_calendar.append((filled, content, ctype or ""))
                continue
            upcoming = filter_upcoming(parsed, now)
            log.info("feed_parsed", kind="calendar", url=filled, count=len(upcoming))
            if upcoming:
                return upcoming[:max_events]
            # calendar parsed but only past events -> keep looking

        # Pass 2 — non-calendar feeds via the LLM listing pass (first non-empty wins).
        for filled, content, _ctype in non_calendar:
            trimmed = content[: self._settings.max_page_chars]
            parsed = self._run_listing(filled, source.platform, trimmed, [])
            log.info("feed_parsed_via_llm", url=filled, count=len(parsed))
            if parsed:
                return parsed[:max_events]
        return []

    # --- merge feed events with listing events ---
    def _merge_events(
        self, listing: list[Any], feed: list[Any]
    ) -> list[Any]:
        """Merge feed events into listing events, de-duplicating by title + date.

        Key is (lowercased title prefix, start-date string). On a duplicate, the
        richer record (more populated fields) wins — a feed event with full
        time/venue replaces a listing stub, and vice-versa. Feed events are
        always appended for the ones not already present, so a JS-only page
        (empty listing) is fully covered by its feed.
        """
        merged: list[Any] = list(listing)
        keys: dict[tuple[str, str], int] = {}
        for i, e in enumerate(merged):
            if isinstance(e, dict):
                keys[self._event_key(e)] = i

        for e in feed:
            if not isinstance(e, dict):
                continue
            k = self._event_key(e)
            if k in keys:
                idx = keys[k]
                existing = merged[idx]
                if isinstance(existing, dict) and self._populated(e) > self._populated(existing):
                    merged[idx] = e
                continue
            keys[k] = len(merged)
            merged.append(e)
        return merged

    @staticmethod
    def _event_key(e: dict[str, Any]) -> tuple[str, str]:
        """Stable de-dup key: (title[:80].lower(), start date YYYY-MM-DD)."""
        title = str(e.get("title") or "").strip().lower()[:80]
        start = e.get("starts_at")
        date = ""
        if isinstance(start, datetime):
            date = start.date().isoformat()
        elif start:
            date = str(start)[:10]
        return (title, date)

    @staticmethod
    def _populated(e: dict[str, Any]) -> int:
        """Count of non-empty fields — used to pick the richer record on dedup."""
        return sum(1 for v in e.values() if v not in (None, "", []))

    # --- backend event_key (run-over-run identity) --------------------------
    #
    # Authoritative ids (iCal/jcal ``uid`` or detail-page ``url``) deliberately
    # EXCLUDE the date: a postponed event keeps the same key, so the backend
    # sees a date change on the same key as a POSTPONED. The ``t:<hash>``
    # fallback (no uid/url) INCLUDES the date so recurring instances without a
    # stable id stay distinct within a run (a title-only key would collide and
    # crash the backend's per-run unique constraint); postpone detection for
    # those is delegated to the backend fuzzy matcher.
    @staticmethod
    def _backend_event_key(ev: dict[str, Any], source_id: int) -> str:
        """Stable per-event identity sent to the backend for reconciliation."""
        uid = ev.get("uid")
        if isinstance(uid, str) and uid.strip():
            return uid.strip()[:200]
        url = ev.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()[:200]
        title = str(ev.get("title") or "").strip().lower()
        date = ""
        s = ev.get("starts_at")
        if isinstance(s, datetime):
            date = s.date().isoformat()
        elif s:
            date = str(s)[:10]
        digest = sha1(f"{title}|{source_id}|{date}".encode()).hexdigest()[:16]
        return f"t:{digest}"

    def _dedup_by_event_key(
        self, events: list[Any], source_id: int, log: Any
    ) -> list[dict[str, Any]]:
        """Final de-dup by backend ``event_key``; stamp it on each survivor.

        On a duplicate key the richer record (more populated fields) wins, so a
        feed event with full venue/time replaces a listing stub. Guarantees the
        backend's per-run ``(source, event_key, run)`` unique constraint is never
        hit (the extractor never submits the same key twice in one run).
        """
        seen: dict[str, int] = {}
        survivors: list[dict[str, Any]] = []
        dropped = 0
        for e in events:
            if not isinstance(e, dict):
                continue
            key = self._backend_event_key(e, source_id)
            e = dict(e)
            e["event_key"] = key
            if key in seen:
                idx = seen[key]
                if self._populated(e) > self._populated(survivors[idx]):
                    survivors[idx] = e
                dropped += 1
            else:
                seen[key] = len(survivors)
                survivors.append(e)
        if dropped:
            log.info("event_key_dedup", dropped=dropped, kept=len(survivors))
        return survivors

    # --- detail-link following ---
    def _enrich_with_details(
        self, events: list[Any], source: DueSource, log: Any
    ) -> list[Any]:
        """Follow distinct per-event detail URLs and merge detail fields in.

        General and site-agnostic: the model decides which events have a detail
        page (via the `url` it returned); we just fetch those URLs. Detail pages
        that fail to fetch/parse leave the listing-level event intact (graceful
        degradation). Bounded by ``max_detail_pages_per_source``.
        """
        max_detail = self._settings.max_detail_pages_per_source
        if max_detail <= 0:
            return events

        source_url, _ = urldefrag(source.url)
        followed = 0
        # Cache of detail_url -> non-empty overrides from that detail page, so
        # multiple listing events pointing at the same detail page share results.
        cache: dict[str, dict[str, Any]] = {}
        enriched: list[Any] = []

        for ev in events:
            if not isinstance(ev, dict):
                enriched.append(ev)
                continue
            detail_url = self._resolve_detail_url(ev.get("url"), source_url)
            if not detail_url:
                # Not a usable detail page (the model returned the source URL,
                # a fragment, or junk). Drop the url so the observation stays
                # honest — an event's url is its own page or absent.
                enriched.append({k: v for k, v in ev.items() if k != "url"})
                continue

            if detail_url in cache:
                enriched.append({**ev, **cache[detail_url], "url": detail_url})
                continue
            if followed >= max_detail:
                log.debug("detail_skipped_cap", url=detail_url, cap=max_detail)
                enriched.append({**ev, "url": detail_url})
                continue

            followed += 1
            overrides = self._extract_detail(detail_url, source, log)
            if overrides:
                cache[detail_url] = overrides
                enriched.append({**ev, **overrides, "url": detail_url})
            else:
                # Keep the listing event; still record its detail URL.
                enriched.append({**ev, "url": detail_url})
        if followed:
            log.info("detail_pages_followed", count=followed, cap=max_detail)
        return enriched

    def _resolve_detail_url(self, url: Any, source_url: str) -> str | None:
        """Return an absolute, fetchable detail URL distinct from the source page."""
        if not isinstance(url, str):
            return None
        url = url.strip()
        if not url or url.startswith("#"):
            return None
        absolute, _ = urldefrag(urljoin(source_url, url) if source_url else url)
        if absolute == source_url:
            return None  # never refetch the source page as a "detail" page
        if not absolute.startswith(("http://", "https://")):
            return None
        return absolute

    def _extract_detail(
        self, detail_url: str, source: DueSource, log: Any
    ) -> dict[str, Any] | None:
        """Fetch one detail page and extract a single event's full fields.

        Returns the non-empty fields from the detail page (to merge over the
        listing event), or None if the page can't be fetched or isn't an event.
        """
        dlog = log.bind(detail_url=detail_url)
        try:
            html = self._fetch_html(detail_url)
        except FetchError as exc:
            dlog.warning("detail_fetch_failed", error=str(exc))
            return None
        # Don't inline links on a detail page: it may list "related events" and
        # we don't want the model chasing them — we only want this one event.
        text, jsonld = self._clean_html(html, source_url=detail_url, preserve_links=False)
        dlog.debug("detail_page_cleaned", chars=len(text), jsonld_blocks=len(jsonld))

        user_message = self._build_user_message(detail_url, source.platform, text, jsonld)
        self._call_model(_DETAIL_SYSTEM_PROMPT, user_message)
        event_dicts = self._extract_events_list()
        if not event_dicts:
            dlog.info("detail_no_event")
            return None
        # The detail page is about one event; take the first well-formed object.
        detail = event_dicts[0]
        if not isinstance(detail, dict):
            return None
        dlog.info("detail_extracted", title=detail.get("title"))
        # Only non-empty values override the listing event; drop empties/None.
        return {
            k: v
            for k, v in detail.items()
            if v not in (None, "") and k != "url"  # url is set by the caller
        }

    # --- prompt assembly ---
    def _build_user_message(
        self, url: str, platform: str | None, text: str, jsonld: list[str]
    ) -> str:
        parts = [
            f"Page URL: {url}",
            f"Platform: {platform or 'unknown'}",
            "",
            "Page text:",
            text,
        ]
        if jsonld:
            parts += ["", "JSON-LD blocks:", *jsonld]
        return "\n".join(parts)


# --- helpers ---


def _coerce_events(payload: Any) -> list[dict[str, Any]]:
    """Normalize a tool-call payload to a list of event dicts."""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        if "events" in payload:
            evs = payload["events"]
            if isinstance(evs, list):
                return [e for e in evs if isinstance(e, dict)]
        # A single event object returned directly.
        return [payload]
    return []


def _parse_json_array_from_content(content: str) -> list[dict[str, Any]]:
    """Best-effort parse of a JSON array out of model message content.

    Handles bare arrays, ```json fenced blocks, and an object wrapping an
    ``events`` key. Returns [] if nothing parseable is found.
    """
    if not content:
        return []
    stripped = content.strip()
    # Strip a ```json ... ``` fence if present.
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        # Try to locate the first '[' ... matching close (best effort).
        start = content.find("[")
        end = content.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(content[start : end + 1])
            except json.JSONDecodeError:
                return []
        else:
            return []
    return _coerce_events(parsed)


def _safe_get(obj: Any, *path: object) -> Any:
    """Attribute-or-index traversal that tolerates dicts, objects, and lists."""
    cur = obj
    for key in path:
        if cur is None:
            return None
        if isinstance(key, int):
            if isinstance(cur, (list, tuple)) and -len(cur) <= key < len(cur):
                cur = cur[key]
            else:
                return None
        else:
            if isinstance(cur, dict):
                cur = cur.get(key)  # type: ignore[arg-type]
            else:
                cur = getattr(cur, key, None)
    return cur