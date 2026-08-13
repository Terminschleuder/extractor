"""The LLM-based extractor.

Fetches a source page with httpx, cleans the HTML with BeautifulSoup, and asks
an **OpenAI-compatible** chat-completion endpoint to return events as
structured JSON via a forced function tool call (``extract_events``). The model
output is **untrusted**: every item is validated with pydantic and bad items
are dropped with a WARNING — one malformed event never crashes a whole source.

Provider portability: we point the ``openai`` SDK at a configurable
``base_url`` (local Ollama by default; any OpenAI-compatible endpoint works).
We do **not** set ``strict: True`` on the tool schema (many compatible backends
ignore it) and we keep a content-JSON fallback for models that emit the events
in the message body instead of via a tool call (common with small Ollama
models). No agentic loop — one forced call, parse, done.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from bs4 import BeautifulSoup

from ..config import Settings
from ..errors import FetchError, LlmError
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
            "description": "Start datetime in ISO-8601, e.g. 2025-09-15T19:00:00.",
        },
        "ends_at": {
            "type": ["string", "null"],
            "description": "Optional end datetime in ISO-8601.",
        },
        "description": {"type": ["string", "null"], "description": "Optional description."},
        "url": {"type": ["string", "null"], "description": "Canonical event URL if shown."},
        "platform": {"type": ["string", "null"], "description": "Observed platform/site name."},
        "attendance_mode": {
            "type": "string",
            "enum": [e.value for e in AttendanceMode],
            "description": "physical | online | hybrid.",
        },
        "event_type": {
            "type": "string",
            "enum": [e.value for e in EventType],
            "description": "meetup | conference | workshop | social | other.",
        },
        "venue_name": {"type": ["string", "null"]},
        "venue_address": {"type": ["string", "null"]},
        "venue_city": {"type": ["string", "null"]},
        "latitude": {"type": ["number", "null"], "description": "Decimal degrees."},
        "longitude": {"type": ["number", "null"], "description": "Decimal degrees."},
    },
    "required": ["title", "starts_at"],
}

EXTRACT_EVENTS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "extract_events",
        "description": (
            "Extract all upcoming events listed on the page as a list of event "
            "objects. Call this tool exactly once with every event you can find. "
            "If there are no events, return an empty list."
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

_SYSTEM_PROMPT = """\
You are an event extraction engine for the terminschleuder event directory.
You are given the cleaned text of a web page (plus any JSON-LD blocks) and the
page URL. Extract every upcoming event you can find and return them via the
`extract_events` tool.

Rules:
- Return ONLY events that have at least a title and a start datetime. If you
  cannot confidently determine either, omit the event.
- `starts_at` and `ends_at` must be ISO-8601 strings with timezone or local
  time as shown on the page (e.g. "2025-09-15T19:00:00").
- `attendance_mode` is one of: physical, online, hybrid. Infer from the page;
  default to physical if unclear.
- `event_type` is one of: meetup, conference, workshop, social, other. Choose
  the best fit; default to other.
- `latitude`/`longitude` are decimal degrees if a venue location is available;
  otherwise omit them. Send them as separate numbers.
- `url` is the event's own page if distinct from the source URL; otherwise omit.
- If the page lists no events at all, return an empty `events` array.
- Do not invent events. Do not include past events. Do not include duplicates.
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

    # --- HTML cleaning ---
    def _clean_html(self, html: str) -> tuple[str, list[str]]:
        """Return (visible_text, jsonld_blocks). Strips noise tags + trims length."""
        soup = BeautifulSoup(html, "html.parser")

        # Collect JSON-LD blocks *before* stripping scripts (JSON-LD lives in
        # <script type="application/ld+json">, which the noise-strip would remove).
        jsonld_blocks: list[str] = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            if script.string:
                jsonld_blocks.append(script.string.strip())

        for tag in soup(list(_NOISE_TAGS)):
            tag.decompose()

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
        text, jsonld = self._clean_html(html)
        log.debug("page_cleaned", chars=len(text), jsonld_blocks=len(jsonld))

        user_parts = [
            f"Source URL: {source.url}",
            f"Platform: {source.platform or 'unknown'}",
            "",
            "Page text:",
            text,
        ]
        if jsonld:
            user_parts += ["", "JSON-LD blocks:", *jsonld]
        user_message = "\n".join(user_parts)

        self._call_model(_SYSTEM_PROMPT, user_message)
        event_dicts = self._extract_events_list()
        log.info("llm_extracted", raw_count=len(event_dicts), model=self._settings.llm_model)

        observations: list[ObservationSubmit] = []
        for idx, item in enumerate(event_dicts):
            if not isinstance(item, dict):
                log.warning("dropping_non_object_event", index=idx)
                continue
            # Stamp provenance the model should not set.
            item = dict(item)
            item.setdefault("source", source.id)
            item.setdefault("platform", source.platform or None)
            item.setdefault("url", source.url)
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