# LLM extraction

The `LLMExtractor` (`src/terminschleuder_extractor/extractors/llm.py`) is the
single built-in extractor. It is **generic** — one implementation handles any
source page; there are no per-site scrapers (by design). It is provider-portable:
it talks to any **OpenAI-compatible** chat-completion endpoint (local Ollama by
default) via the `openai` SDK pointed at `EXTRACTOR_LLM_BASE_URL`.

## Pipeline

1. **Fetch** — `httpx.get(source.url)` with a descriptive `User-Agent` and the
   configured timeout; raise `FetchError` on non-2xx or transport error.
2. **Clean** — parse with BeautifulSoup (`html.parser`, stdlib — no lxml).
   First collect `<script type="application/ld+json">` blocks verbatim (they
   often carry structured `Event` data), then strip `script`, `style`,
   `noscript`, `nav`, `footer`, `header`, `aside`, `form`, `svg`. Extract the
   remaining visible text, collapse blank lines, and truncate to
   `EXTRACTOR_MAX_PAGE_CHARS` (default 20000) to bound cost.
3. **Call** — `client.chat.completions.create(...)` with:
   - `model`, `max_tokens`, `temperature=0.0`;
   - one function tool `extract_events` (schema below);
   - `tool_choice={"type":"function","function":{"name":"extract_events"}}`
     — the call is **forced** (the user asked for "forcing a specific tool
     call"). Some providers reject a forced `tool_choice`; if so, the fix is
     on the operator side (use a tool-capable model/endpoint). We deliberately
     do **not** set `strict: true` on the schema — Ollama and many compatible
     backends ignore it, so we rely on pydantic validation downstream.
4. **Parse** — read `choices[0].message.tool_calls[0].function.arguments` (a
   JSON string), `json.loads` it, and coerce to a list of event dicts. If the
   model returned **no tool call**, fall back to parsing a JSON array out of
   `message.content` (handles bare arrays and ```json fenced blocks). This
   fallback matters: small local models often emit the events in the content
   instead of via a tool call.
5. **Validate** — coerce each event dict into `ObservationSubmit` with
   `source=source.id`, `platform=source.platform or None`, `url=source.url` as
   defaults. A pydantic `ValidationError` drops that one event with a WARNING
   log; the rest survive. An empty result → `[]`.

No agentic loop — one forced call (or one content parse), validate, done.

## System prompt

The prompt (in `llm.py`, `SYSTEM_PROMPT`) instructs the model to:

- return only events with at least a title and a start datetime;
- use ISO-8601 for `starts_at`/`ends_at`;
- pick `attendance_mode` (`physical|online|hybrid`, default physical) and
  `event_type` (`meetup|conference|workshop|social|other`, default other);
- send `latitude`/`longitude` as separate numbers when available;
- return an empty `events` array when the page lists none;
- never invent events, never include past events, never duplicate.

## Tool schema

The tool describes an object with an `events` array; each event is an object
with the observation fields and the enum values. `required` is `["title",
"starts_at"]` per event and `["events"]` at the top level. The full schema is
`EXTRACT_EVENTS_TOOL` / `_EVENT_OBJECT_SCHEMA` in `llm.py`; keep it in sync with
`ObservationSubmit` and the backend serializer when either changes.

## Why untrusted output is fine

The backend forces every submitted observation to `pending`; an operator
reviews and promotes in the admin. So a hallucinated or malformed event is at
worst noise for a reviewer to reject, never a canonical event. We still
minimize that noise by validating with pydantic and dropping bad items, but
we never let one bad item fail an otherwise-good source's run.

## Cost notes

- Page text is truncated to `EXTRACTOR_MAX_PAGE_CHARS` (20000) before prompting.
- `temperature=0` for deterministic-ish extraction.
- One model call per source per cycle (no multi-turn loop).
- For high volume, set `EXTRACTOR_LLM_MODEL` to a cheaper/faster model your
  endpoint serves (e.g. `qwen2.5`, `gpt-4o-mini`); the choice is the operator's.
- Default cadence is one cycle/hour and one crawl/site/hour, so steady-state
  call volume is low; it scales with the number of due sources, not with wall
  clock.