"""Typed HTTP client for the ingestion API.

A thin wrapper over ``httpx.Client`` that:
  * authenticates with ``Authorization: Api-Key <key>`` (see ``auth.py``),
  * speaks the backend's trailing-slash DRF routes,
  * follows ``next`` links across page-number pagination, and
  * maps non-2xx responses to ``ApiError`` (with the DRF error body for logs).

All paths are relative to ``base_url`` and end with ``/`` (DRF trailing slash).
The caller never sees raw httpx objects — only typed model instances.
"""

from __future__ import annotations

from typing import Any

import httpx

from .auth import APIKeyAuth
from .errors import ApiError, AuthError
from .models import DueSource, IngestionRun, ObservationSubmit

# Ingestion routes (all under {base_url}/api/ingestion/).
SOURCES_DUE_PATH = "/api/ingestion/sources/due/"
RUNS_PATH = "/api/ingestion/runs/"
OBSERVATIONS_BULK_PATH = "/api/ingestion/observations/bulk/"

# Max page size the backend allows (StandardPagination.max_page_size).
MAX_PAGE_SIZE = 1000


class TerminschleuderClient:
    """Client for the extractor-facing ingestion API."""

    def __init__(
        self,
        base_url: str,
        auth: APIKeyAuth,
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        if client is not None:
            # Test injection: a (mocked) httpx client, e.g. respx.
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                headers=auth.headers(),
                timeout=timeout,
                follow_redirects=False,
            )
            self._owns_client = True

    # --- lifecycle ---
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> TerminschleuderClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- low level ---
    def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> Any:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        try:
            resp = self._client.request(method, url, json=json)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"transport error: {exc}") from exc
        if resp.status_code == 401:
            raise AuthError(f"authentication rejected (401): {resp.text}")
        if resp.status_code == 403:
            # The backend uses Api-Key auth; a 403 usually means the key is
            # missing/revoked or the account lacks ingestion perms.
            raise AuthError(f"forbidden (403): {resp.text}")
        if not (200 <= resp.status_code < 300):
            try:
                detail: Any = resp.json()
            except ValueError:
                detail = resp.text
            raise ApiError(resp.status_code, detail)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # --- work queue ---
    def get_due_sources(self) -> list[DueSource]:
        """Return all due sources, following pagination via ``next``."""
        results: list[dict[str, Any]] = []
        url: str | None = f"{SOURCES_DUE_PATH}?page_size={MAX_PAGE_SIZE}"
        while url:
            page = self._request("GET", url)
            results.extend(page.get("results", []))
            next_url = page.get("next")
            # ``next`` is an absolute URL; the _request handles that.
            url = next_url if next_url else None
        return [DueSource.model_validate(item) for item in results]

    # --- runs ---
    def create_run(self, source_id: int) -> IngestionRun:
        """Open a run for a source (defaults to running; backend stamps now)."""
        data = self._request("POST", RUNS_PATH, json={"source": source_id})
        return IngestionRun.model_validate(data)

    def finish_run_success(self, run_id: int, events_found: int) -> IngestionRun:
        path = f"{RUNS_PATH}{run_id}/success/"
        data = self._request("POST", path, json={"events_found": events_found})
        return IngestionRun.model_validate(data)

    def finish_run_failure(self, run_id: int, error_message: str) -> IngestionRun:
        path = f"{RUNS_PATH}{run_id}/failure/"
        data = self._request("POST", path, json={"error_message": error_message})
        return IngestionRun.model_validate(data)

    # --- observations ---
    def submit_observations(
        self, observations: list[ObservationSubmit]
    ) -> list[dict[str, Any]]:
        """Bulk-submit observations (transactional on the backend)."""
        if not observations:
            return []
        payload = {"observations": [obs.to_api() for obs in observations]}
        data = self._request("POST", OBSERVATIONS_BULK_PATH, json=payload)
        if isinstance(data, list):
            return data
        # Defensive: some schemas wrap; accept a dict with ``results``/``observations``.
        if isinstance(data, dict):
            return data.get("results") or data.get("observations") or [data]
        return []

    # --- diagnostics ---
    def ping(self) -> bool:
        """Lightweight connection/auth check: fetch one due source page."""
        try:
            self._request("GET", f"{SOURCES_DUE_PATH}?page_size=1")
            return True
        except (ApiError, AuthError):
            return False