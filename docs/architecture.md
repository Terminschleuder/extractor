# Architecture

The extractor is the *producer* side of the terminschleuder ingestion contract:
the backend (`/api/ingestion/...`) is the consumer-facing surface an external
extraction system uses to discover due sources, report runs, and submit
untrusted observations. This repo implements that external system.

## Flow

```
            ┌───────────────┐   GET /api/ingestion/sources/due/        ┌───────────────┐
            │   runner      │ ───────────────────────────────────────▶ │  terminschleuder  │
            │  (loop/once)  │                                          │   backend (DRF)   │
            └──────┬────────┘   POST /api/ingestion/runs/              └───────────────┘
                   │              POST /api/ingestion/runs/{id}/success|failure/
                   │              POST /api/ingestion/observations/bulk/
                   ▼
            ┌───────────────┐  httpx GET source.url ──▶ HTML
            │  extractor    │  BeautifulSoup clean ──▶ readable text + JSON-LD
            │  (registry)   │  openai SDK (base_url) ─▶ chat.completions tool_call → JSON
            └───────────────┘  pydantic validate ───▶ list[ObservationSubmit]
```

One cycle (`Runner.run_once`):

1. `client.get_due_sources()` — fetch due sources (paginated).
2. For each source (capped at `max_sources_per_cycle`):
   - **Politeness floor**: skip if last-crawled is within
     `min_source_interval_seconds` (see below).
   - `client.create_run(source.id)` — open a run (status `running`).
   - `extractor.extract(source)` — fetch + clean + LLM → observations.
   - Stamp each observation's `run` with the run id.
   - `client.submit_observations(observations)` — bulk submit (transactional).
   - `client.finish_run_success(run.id, events_found=len(obs))`.
   - On any exception: `client.finish_run_failure(run.id, error_message=...)`.
3. In loop mode, sleep `poll_interval_seconds` and repeat; SIGTERM/SIGINT set
   a stop flag, the in-flight cycle finishes, then the process exits.

## Components

- **`config.py`** — `Settings` (pydantic-settings, `EXTRACTOR_*` env). Lazy
  validation: secrets raise only when used, so `--self-test`/`--dry-run` work
  without them.
- **`auth.py`** — `APIKeyAuth` builds the `Authorization: Api-Key <key>` header.
- **`client.py`** — `TerminschleuderClient`: typed wrapper over `httpx.Client`
  with DRF pagination following and `ApiError`/`AuthError` mapping.
- **`models.py`** — pydantic v2 shapes mirroring the backend serializers. The
  key one is `ObservationSubmit.to_api()`, which emits exactly the submit
  serializer's field set (lat/lon as separate floats, no `status`).
- **`extractors/base.py`** — `Extractor` ABC + `@register(name)` registry;
  `get_extractor(platform)` resolves with an `llm` fallback.
- **`extractors/llm.py`** — `LLMExtractor`: httpx fetch, BeautifulSoup clean,
  forced OpenAI-compatible `extract_events` tool call, content-JSON fallback,
  per-item pydantic validation. See [llm-extraction.md](llm-extraction.md).
- **`runner.py`** — `Runner` + `CrawlGate` (the per-source floor). Cycle
  orchestration, signal handling, interruptible sleep.
- **`logging_setup.py`** — structlog (console or JSON). Never logs secrets or
  raw payloads — counts, ids, and status only.
- **`__main__.py`** — CLI dispatcher.

## Cadence (two independent knobs)

The backend's `/sources/due/` is the *primary* per-source cadence: it only
returns sources whose `next_due_at` has passed (set per-source by
`fetch_interval_minutes`, advanced when a run finishes). On top of that:

- **`poll_interval_seconds`** (default 3600) — how often the *runner* wakes up
  to ask the backend for due sources.
- **`min_source_interval_seconds`** (default 3600) — a client-side per-source
  floor: never crawl the same source more often than this, even if the backend
  reports it due and the cycle is shorter.

Together: by default one cycle per hour and each site at most hourly. Set
`poll_interval_seconds=300` to react to *newly-due* sources within 5 minutes
without raising the per-site rate above the floor.

`CrawlGate` tracks `last_crawled_at` per source id, optionally persisted to
`state_file` (JSON: `{source_id: iso8601}`) so a restart doesn't immediately
re-crawl everything.

## Configuration

All settings live in `config.py` with the `EXTRACTOR_` env prefix; see the
README env table for the full list. Defaults make the extractor polite
(one cycle/h, one crawl/site/h) and point the LLM at local Ollama.

## Deployment

Container-first. `Dockerfile` builds a `python:3.14-slim-bookworm` image,
non-root, no GIS deps. `docker-compose.yml` runs it with `restart: unless-stopped`,
mounts a state volume at `/app/state`, and adds `host.docker.internal:host-gateway`
so the container can reach a host-side Ollama at `http://host.docker.internal:11434/v1`.
`start.sh` builds and runs (loop by default; pass flags for one-offs).

The host `.venv` exists only for tests — prod always runs in the container.

## Failure handling

- A non-2xx ingestion response raises `ApiError`/`AuthError`; if it happens
  while fetching due sources, the cycle logs and exits non-zero (loop retries
  next interval).
- If `create_run` fails, the source is skipped (no run to mark failed).
- If extraction or submission fails, the run is marked `failed` with the error
  message; the cycle continues with the next source.
- A single malformed LLM event is dropped (WARNING log); it never fails the run.