# terminschleuder-extractor

An **LLM-based extractor** for the [terminschleuder](https://github.com/Terminschleuder)
event directory. It polls the backend's ingestion API for due sources, fetches
each page, asks an **OpenAI-compatible** model (local Ollama by default, or any
other provider) to extract events as structured JSON, and submits them back as
**pending** observations for human review.

- **Generic, not page-specific.** One LLM extractor handles any source page;
  a pluggable `Extractor` registry is the extension point if you ever need a
  specialized one. (No per-site scrapers, by design.)
- **Container-first.** Runs as a Docker service from day one; a host venv is
  included only so tests run locally.
- **Polite by default.** One cycle per hour, and each site is crawled at most
  once per hour even if the cycle is shortened.

## Quickstart

### Tests (host venv — no secrets, no network)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q            # 51 tests, ~0.2s
```

### Run in Docker

```bash
cp .env.example .env           # fill in EXTRACTOR_API_KEY (and LLM endpoint)
./start.sh                     # build + run the loop (one cycle/hour)
```

One-off:

```bash
./start.sh --self-test         # verify wiring without secrets/network → prints OK
./start.sh --dry-run --log-level DEBUG   # list due sources, create no runs
./start.sh --once              # run a single cycle and exit
```

### Run from the host venv (development)

```bash
source .venv/bin/activate
EXTRACTOR_API_KEY=... EXTRACTOR_LLM_BASE_URL=http://localhost:11434/v1 \
  python -m terminschleuder_extractor --once --log-level DEBUG
```

## CLI

```
python -m terminschleuder_extractor [--once | --dry-run | --self-test]
  [--log-level LEVEL] [--log-format console|json]
  [--max-sources N] [--poll-interval SECONDS] [--min-source-interval SECONDS]
```

| Flag | Effect | Env override |
|---|---|---|
| *(default)* | run the loop: one cycle per `poll-interval` | `EXTRACTOR_RUN_MODE=loop` |
| `--once` | run a single cycle and exit | `EXTRACTOR_RUN_MODE=once` |
| `--dry-run` | list due sources (with would-crawl status); create no runs | — |
| `--self-test` | verify config/auth/extractor wiring without secrets/network | — |
| `--log-level` | `DEBUG`/`INFO`/`WARNING`/… | `EXTRACTOR_LOG_LEVEL` |
| `--log-format` | `console` or `json` | `EXTRACTOR_LOG_FORMAT` |
| `--max-sources` | cap sources processed per cycle | `EXTRACTOR_MAX_SOURCES_PER_CYCLE` |
| `--poll-interval` | seconds between cycles (loop mode) | `EXTRACTOR_POLL_INTERVAL_SECONDS` |
| `--min-source-interval` | per-source politeness floor in seconds | `EXTRACTOR_MIN_SOURCE_INTERVAL_SECONDS` |

## Configuration (env vars, prefix `EXTRACTOR_`)

| Var | Default | Meaning |
|---|---|---|
| `EXTRACTOR_API_BASE_URL` | `https://www.terminschleuder.online` | Backend base URL |
| `EXTRACTOR_API_KEY` | *(empty — required for real runs)* | Sent as `Authorization: Api-Key <key>` |
| `EXTRACTOR_RUN_MODE` | `loop` | `loop` or `once` |
| `EXTRACTOR_POLL_INTERVAL_SECONDS` | `3600` | How often the runner asks the backend for due sources |
| `EXTRACTOR_MIN_SOURCE_INTERVAL_SECONDS` | `3600` | Never crawl the same source more often than this |
| `EXTRACTOR_MAX_SOURCES_PER_CYCLE` | `20` | Cap per cycle |
| `EXTRACTOR_STATE_FILE` | *(empty)* | Path to persist crawl timestamps across restarts |
| `EXTRACTOR_HTTP_TIMEOUT_SECONDS` | `30` | HTTP timeout (fetch + API) |
| `EXTRACTOR_USER_AGENT` | `terminschleuder-extractor/0.1` | User-Agent for source fetches |
| `EXTRACTOR_MAX_PAGE_CHARS` | `20000` | Truncate cleaned page text to bound LLM cost |
| `EXTRACTOR_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible endpoint (Ollama default) |
| `EXTRACTOR_LLM_API_KEY` | `ollama` | SDK requires non-empty; Ollama ignores it |
| `EXTRACTOR_LLM_MODEL` | `llama3.1` | Model served by the endpoint |
| `EXTRACTOR_LLM_MAX_TOKENS` | `4096` | Max response tokens |
| `EXTRACTOR_LLM_TEMPERATURE` | `0.0` | Sampling temperature |
| `EXTRACTOR_LOG_LEVEL` | `INFO` | Log level |
| `EXTRACTOR_LOG_FORMAT` | `console` | `console` or `json` |

> **From a container**, a host-side Ollama is reachable at
> `http://host.docker.internal:11434/v1` (the compose file already adds
> `host-gateway`). There is **no** `ANTHROPIC_API_KEY` — the extractor speaks
> the OpenAI-compatible API only.

## Ingestion API surface used

All under `{API_BASE_URL}/api/ingestion/` (auth: `Api-Key`).

| Method + path | Body | Purpose |
|---|---|---|
| `GET /sources/due/` | — | Work queue: approved/active/due sources (paginated) |
| `POST /runs/` | `{"source": <id>}` | Open a run (status defaults to `running`) |
| `POST /runs/<id>/success/` | `{"events_found": N}` | Mark run succeeded |
| `POST /runs/<id>/failure/` | `{"error_message": "..."}` | Mark run failed |
| `POST /observations/bulk/` | `{"observations": [...]}` | Submit observations (transactional; forced `pending`) |

Observation fields: `source` (req), `run`, `title` (req, ≤200), `starts_at`
(req, ISO-8601), `ends_at`, `description`, `url` (≤200), `platform` (≤80),
`attendance_mode` (`physical|online|hybrid`), `event_type`
(`meetup|conference|workshop|social|other`), `venue_name`, `venue_address`,
`venue_city`, `latitude`, `longitude` (separate floats — backend builds
`Point(lon, lat)`), `raw_payload`. `status` is **never** sent.

## How LLM extraction works

For each due source: fetch the page with httpx → clean with BeautifulSoup
(strip `script`/`style`/`nav`/etc., collect JSON-LD blocks) → call the model
with a **forced** `extract_events` function tool (`tool_choice` forced) →
parse the tool arguments as a list of events → validate each with pydantic →
submit the valid ones. If a model returns no tool call, fall back to parsing a
JSON array from the message content (some local models do this). Model output
is always untrusted: a malformed event is dropped, never crashes the source.

See `docs/llm-extraction.md` for the full prompt/tool schema and cost notes.

## Adding an extractor

The `Extractor` ABC (`src/.../extractors/base.py`) + `@register("name")`
registry is the extension point. The runner resolves by the source's
`platform`, falling back to the `llm` default. Add a class, decorate it, and
it drops in without touching the runner. (Today only `llm` ships, by design.)

## Docs

- [`docs/architecture.md`](docs/architecture.md) — design, components, flow, deployment
- [`docs/api-client.md`](docs/api-client.md) — the ingestion API surface in depth
- [`docs/llm-extraction.md`](docs/llm-extraction.md) — prompt, tool schema, validation, cost

See [`AGENTS.md`](AGENTS.md) for the contribution invariants (code/tests/README/docs kept in sync).