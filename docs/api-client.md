# Ingestion API client

The extractor talks to the terminschleuder backend's ingestion surface, mounted
at `/api/ingestion/`. Everything is authenticated with a long-lived API key
sent as `Authorization: Api-Key <raw-key>` (the backend's `APIKeyAuthentication`
in `accounts/authentication.py`). The caller is a service account in an
`ingestion` group; the backend gates every route with `IsIngestionService`.

`TerminschleuderClient` (`src/terminschleuder_extractor/client.py`) is a thin
typed wrapper over `httpx.Client`. It owns the auth header, speaks the
trailing-slash DRF routes, follows page-number pagination, and maps non-2xx
responses to `ApiError` (or `AuthError` on 401/403).

## Endpoints

Base path: `{api_base_url}/api/ingestion/`. All routes end with `/` (DRF).

### `GET /sources/due/` — the work queue

Approved, active sources whose `next_due_at` has passed (or is null — never
fetched). DRF page-number pagination: `?page_size=` (max 1000); the response is
`{count, next, previous, results}`. The client follows `next` (an absolute URL)
and returns a flat `list[DueSource]`.

`DueSource` fields: `id`, `organization {id,name,slug}`, `url`, `platform`,
`fetch_interval_minutes`, `last_fetched_at`, `next_due_at`.

### `POST /runs/` — open a run

Body: `{"source": <id>}`. `started_at` is optional (the backend stamps `now`
if omitted); `status` defaults to `running`; `reported_by` is set server-side
to the calling service account. Returns `IngestionRun`.

`IngestionRun` fields: `id`, `source`, `started_at`, `finished_at`, `status`
(`pending|running|succeeded|failed`), `events_found`, `events_promoted`,
`error_message`, `created_at`.

### `POST /runs/<id>/success/` — mark succeeded

Body: `{"events_found": N}`. The backend sets `status=succeeded`,
`finished_at=now`, and advances the source's `last_fetched_at`/`next_due_at`
(`now + fetch_interval_minutes`).

### `POST /runs/<id>/failure/` — mark failed

Body: `{"error_message": "..."}`. The backend sets `status=failed`,
`finished_at=now`, and still advances the source schedule (so a failed source
isn't retried immediately on the next cycle).

### `POST /observations/bulk/` — submit observations

Body: `{"observations": [ObservationSubmit.to_api(), ...]}`. Created
transactionally (`transaction.atomic`). Each observation is forced to
`status=pending` server-side — the extractor can never self-promote. Returns
the created observations (full read serializer, with resolved `location`).

## Observation payload (`ObservationSubmit.to_api()`)

Mirrors the backend `EventObservationSubmitSerializer` (`events/
ingestion_serializers.py`). `None` optionals are omitted.

| Field | Type | Required | Notes |
|---|---|---|---|
| `source` | int | yes | Which source the observation came from |
| `run` | int | no | Set by the runner from the run it opened |
| `title` | str | yes | ≤200 chars |
| `starts_at` | datetime (ISO-8601) | yes | e.g. `2025-09-15T19:00:00` |
| `ends_at` | datetime | no | ISO-8601 |
| `description` | str | no | Free text |
| `url` | str | no | ≤200; observed event URL |
| `platform` | str | no | ≤80; observed platform/site name |
| `attendance_mode` | enum | no | `physical` \| `online` \| `hybrid` |
| `event_type` | enum | no | `meetup` \| `conference` \| `workshop` \| `social` \| `other` |
| `venue_name` | str | no | ≤200 |
| `venue_address` | str | no | ≤255 |
| `venue_city` | str | no | ≤100 |
| `latitude` | float | no | Decimal degrees |
| `longitude` | float | no | Decimal degrees; backend builds `Point(lon, lat)` |
| `raw_payload` | object | no | Arbitrary JSON, kept for provenance |

`status` is **never** sent — the backend forces `pending`.

## Geometry

The backend stores `location` as a PostGIS `Point` built from the
write-only `latitude`/`longitude` fields as `Point(lon, lat)` (longitude
first). The extractor sends two separate floats and lets the backend assemble
the point — it has no GIS dependency of its own.

## Errors

- `401` / `403` → `AuthError` (missing/revoked key, or the account lacks
  ingestion permissions).
- Other non-2xx → `ApiError(status, detail)` where `detail` is the parsed DRF
  error body (string or field-error dict), used for logging only.
- Transport errors (DNS, timeout, connection refused) → `ApiError(0, ...)`.