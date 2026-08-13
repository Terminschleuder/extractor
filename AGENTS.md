# AGENTS.md — instructions for AI agents working on the extractor

> Read this **before** making any change to this codebase. It captures the rules
> that are easy to forget and the invariants that must not silently break. It
> mirrors the conventions of the sibling `backend/` and `frontend/` repos but
> notes where this repo differs.

## The one rule that matters most

**Every code change must keep the tests, the README, and the docs in sync.**

A change is not "done" when the code imports. It is done only when **all four**
are true:

1. **Code** — the change is implemented.
2. **Tests** — existing tests still pass, and new behavior is covered by new tests.
3. **README** — the quickstart / CLI table / env table reflect the change.
4. **`docs/`** — the functional documentation reflects the change.

If you touch an endpoint, an env var, a model field, the LLM tool schema, the
runner cadence, or the container setup, ask yourself for each of the four:
*"Does this need to change? Does this need a new test?"* If you're not sure,
assume yes.

### What lives where

- `README.md` — the **quickstart** (run it, test it, configure it) + concise CLI
  and env tables. Keep it scannable; push depth into `docs/`.
- `docs/` — the **functional documentation** (architecture, the ingestion API
  client, LLM extraction). See `docs/README.md` for the index.
- `tests/` — the **behavioral spec**. Tests run on the host venv (no GIS, no
  network — httpx is mocked with respx, the OpenAI client is faked).

### Sync checklist (run through it before declaring a task complete)

- [ ] Ingestion endpoint / request body / response field change → updated
      `docs/api-client.md` **and** the README API table?
- [ ] Env var / Settings field / default change → updated `docs/architecture.md`
      **and** the README env table **and** `.env.example`?
- [ ] Runner cadence / per-source floor / state-file change → updated
      `docs/architecture.md` and README?
- [ ] LLM tool schema / system prompt / parsing fallback change → updated
      `docs/llm-extraction.md`?
- [ ] New behavior → **new test(s)** added and passing?
- [ ] README quickstart commands still accurate?

## Container-first; host venv is for tests only

- The **prod image** is `python:3.14-slim-bookworm` (no GDAL/GEOS/PROJ — the
  extractor has no GIS deps; the backend owns the PostGIS Point). Build with
  `docker compose build extractor`; run with `./start.sh`.
- The **host `.venv`** exists only so `pytest` runs on the host without a
  container (unlike the backend, the extractor has no GIS deps, so this is
  safe). Do **not** install anything system-wide on the host — only via
  `pip`/`venv`/Docker.
- HTML parsing uses BeautifulSoup with the stdlib `html.parser` (no `lxml`)
  so the install stays free of compiled C deps — don't reintroduce `lxml`
  unless a wheel is confirmed for the target Python.

## How to run things

```bash
# Host tests (no secrets, no network) — the primary verification gate
source .venv/bin/activate
python -m pytest -q

# Run the loop in Docker (default; restarts on failure)
./start.sh

# One-off commands
./start.sh --once                 # single cycle, then exit
./start.sh --dry-run --log-level DEBUG
./start.sh --self-test            # wiring check without secrets/network

# Build only
docker compose build extractor
```

## Verification gate (do this before committing)

```bash
python -m pytest -q          # all green (host venv)
```

If this fails, the change is not complete — fix it before committing.

## Known gotchas (don't re-learn these the hard way)

- **Ingestion routes end with a trailing slash** (DRF): `/api/ingestion/runs/`,
  `/api/ingestion/runs/<id>/success/`, `/api/ingestion/observations/bulk/`.
  Don't drop the slash — DRF 301-redirects otherwise (and our client has
  `follow_redirects=False`).
- **Auth keyword is `Api-Key`** (not `Bearer`): `Authorization: Api-Key <raw-key>`.
  See the backend `accounts/authentication.py`.
- **Send `latitude`/`longitude` as separate floats**, not a `location` point.
  The backend builds `Point(lon, lat)` (longitude first!) from them. Never
  send `status` — the backend forces observations to `pending`.
- **DRF pagination**: the client follows the `next` link (an absolute URL) and
  reads `results`. Page size is capped at 1000 (`?page_size=`, `StandardPagination`).
- **LLM output is untrusted.** Every event the model returns is validated with
  pydantic and malformed items are dropped with a WARNING — one bad event must
  never crash a whole source. The runner catches extraction failures and
  reports them via `finish_run_failure` (never raises out of a cycle).
- **Never log secrets or raw payloads.** structlog logs counts, ids, and
  status only — never the API key, the `Authorization` header, the full page
  text, or the full model response.
- **The LLM is OpenAI-compatible, not the Anthropic SDK.** Point
  `EXTRACTOR_LLM_BASE_URL`/`_API_KEY`/`_MODEL` at Ollama or any compatible
  provider. From a container reaching host Ollama use
  `http://host.docker.internal:11434/v1` (compose already adds
  `host-gateway`). There is no `ANTHROPIC_API_KEY`, no `thinking`, no `strict`
  tool input — by the user's explicit decision.
- **Cadence is two independent knobs.** `EXTRACTOR_POLL_INTERVAL_SECONDS`
  (cycle length, default 1h) and `EXTRACTOR_MIN_SOURCE_INTERVAL_SECONDS`
  (per-source floor, default 1h). Shortening the cycle never raises the
  per-site crawl rate. The backend's own `next_due_at` is the *primary*
  per-source cadence; the client floor is a belt-and-suspenders guard.
- **respx query matching**: when a mock URL pattern has no query string, respx
  matches any query; when it has one, it matches that query exactly. If you
  mock a pagination `next` URL, include the query string or the loop never
  terminates (see `test_get_due_sources_pagination`).

## Commit conventions

- Keep commits focused; describe *what and why*.
- End commit messages with:
  `Co-Authored-By: Claude <noreply@anthropic.com>`
- The default branch is `main`. No feature branches for now — commit and push
  to `origin/main` after each change with green tests. Only push when the work
  is in a committable state.

## Project layout (cheat sheet)

```
src/terminschleuder_extractor/
  __main__.py     CLI entry point
  config.py       Settings (EXTRACTOR_* env)
  auth.py         Api-Key header
  errors.py       ApiError/AuthError/FetchError/LlmError
  client.py       TerminschleuderClient (ingestion API)
  models.py       enums + DueSource/IngestionRun/ObservationSubmit
  logging_setup.py  structlog (console|json)
  extractors/
    base.py       Extractor ABC + registry
    llm.py        LLMExtractor (httpx + BeautifulSoup + openai tool call)
  runner.py       cycle orchestration + per-source floor
tests/            respx + fake OpenAI client — host venv, no network
docs/             functional documentation
Dockerfile / docker-compose.yml / start.sh   container-first deploy
```

When in doubt: run the tests, read `docs/`, and keep all four (code / tests /
README / docs) in step.