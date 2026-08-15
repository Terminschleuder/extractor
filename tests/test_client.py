"""Tests for the ingestion API client (httpx mocked with respx)."""

from __future__ import annotations

import pytest
import respx

from terminschleuder_extractor.auth import APIKeyAuth
from terminschleuder_extractor.client import (
    OBSERVATIONS_BULK_PATH,
    RUNS_PATH,
    SOURCES_DUE_PATH,
    TerminschleuderClient,
)
from terminschleuder_extractor.errors import ApiError, AuthError
from terminschleuder_extractor.models import ObservationSubmit

BASE = "https://api.test"
AUTH_HEADER = {"Authorization": "Api-Key test-key"}


@pytest.fixture
def client():
    c = TerminschleuderClient(BASE, APIKeyAuth("test-key"))
    yield c
    c.close()


@respx.mock
def test_auth_header_sent(client):
    respx.get(f"{BASE}{SOURCES_DUE_PATH}").mock(
        return_value=respx.MockResponse(200, json={"count": 0, "next": None, "results": []})
    )
    client.get_due_sources()
    sent_headers = respx.calls[0].request.headers
    assert sent_headers["Authorization"] == "Api-Key test-key"


@respx.mock
def test_get_due_sources_pagination(client, due_source_payload):
    page1 = {
        "count": 2,
        "next": f"{BASE}{SOURCES_DUE_PATH}?page=2&page_size=1000",
        "previous": None,
        "results": due_source_payload["results"],
    }
    page2 = {
        "count": 2,
        "next": None,
        "previous": None,
        "results": [
            {
                "id": 43,
                "organization": {"id": 8, "name": "Boo", "slug": "boo"},
                "url": "https://b.test/e",
                "platform": None,
                "fetch_interval_minutes": 30,
                "last_fetched_at": None,
                "next_due_at": None,
            }
        ],
    }
    # Include the query string in each route so respx matches exactly (respx
    # ignores query when the pattern has none — without this both requests
    # match page1 and the ``next`` loop never terminates).
    respx.get(f"{BASE}{SOURCES_DUE_PATH}?page_size=1000").mock(
        return_value=respx.MockResponse(200, json=page1)
    )
    respx.get(f"{BASE}{SOURCES_DUE_PATH}?page=2&page_size=1000").mock(
        return_value=respx.MockResponse(200, json=page2)
    )
    sources = client.get_due_sources()
    assert [s.id for s in sources] == [42, 43]
    assert sources[1].platform is None


@respx.mock
def test_create_run(client):
    respx.post(f"{BASE}{RUNS_PATH}").mock(
        return_value=respx.MockResponse(
            201,
            json={
                "id": 100,
                "source": 42,
                "started_at": "2025-01-01T12:00:00",
                "finished_at": None,
                "status": "running",
                "events_found": 0,
                "events_promoted": 0,
                "error_message": "",
                "created_at": "2025-01-01T11:59:00",
            },
        )
    )
    run = client.create_run(42)
    assert run.id == 100
    assert run.status.value == "running"
    body = respx.calls[0].request.read()
    assert b'"source":42' in body


@respx.mock
def test_finish_run_success(client):
    respx.post(f"{BASE}{RUNS_PATH}100/success/").mock(
        return_value=respx.MockResponse(
            200,
            json={
                "id": 100,
                "source": 42,
                "status": "succeeded",
                "events_found": 3,
            },
        )
    )
    run = client.finish_run_success(100, 3)
    assert run.status.value == "succeeded"
    assert run.events_found == 3
    assert b'"events_found":3' in respx.calls[0].request.read()


@respx.mock
def test_finish_run_failure(client):
    respx.post(f"{BASE}{RUNS_PATH}100/failure/").mock(
        return_value=respx.MockResponse(
            200, json={"id": 100, "source": 42, "status": "failed", "error_message": "boom"}
        )
    )
    run = client.finish_run_failure(100, "boom")
    assert run.status.value == "failed"
    assert b'"error_message":"boom"' in respx.calls[0].request.read()


@respx.mock
def test_submit_observations_empty_is_noop(client):
    assert client.submit_observations([]) == []
    assert respx.calls == []


@respx.mock
def test_submit_observations_bulk(client):
    from datetime import datetime

    obs = ObservationSubmit(
        source=42, event_key="evt-1@rzl.de", title="PyGraten",
        starts_at=datetime(2025, 9, 15, 19, 0, 0),
    )
    respx.post(f"{BASE}{OBSERVATIONS_BULK_PATH}").mock(
        return_value=respx.MockResponse(201, json=[{"id": 1}])
    )
    out = client.submit_observations([obs])
    assert out == [{"id": 1}]
    body = respx.calls[0].request.read()
    assert b'"observations"' in body
    assert b'"source":42' in body
    assert b'"event_key":"evt-1@rzl.de"' in body
    assert b'"status"' not in body  # we never send status


@respx.mock
def test_api_error_on_400(client):
    respx.get(f"{BASE}{SOURCES_DUE_PATH}").mock(
        return_value=respx.MockResponse(400, json={"detail": "bad request"})
    )
    with pytest.raises(ApiError) as exc:
        client.get_due_sources()
    assert exc.value.status == 400


@respx.mock
def test_auth_error_on_401(client):
    respx.get(f"{BASE}{SOURCES_DUE_PATH}").mock(return_value=respx.MockResponse(401, text="nope"))
    with pytest.raises(AuthError):
        client.get_due_sources()


@respx.mock
def test_auth_error_on_403(client):
    respx.get(f"{BASE}{SOURCES_DUE_PATH}").mock(return_value=respx.MockResponse(403, text="nope"))
    with pytest.raises(AuthError):
        client.get_due_sources()


@respx.mock
def test_ping_true_on_success(client):
    respx.get(url__regex=rf"{BASE}{SOURCES_DUE_PATH}.*").mock(
        return_value=respx.MockResponse(200, json={"results": []})
    )
    assert client.ping() is True


@respx.mock
def test_ping_false_on_401(client):
    respx.get(url__regex=rf"{BASE}{SOURCES_DUE_PATH}.*").mock(
        return_value=respx.MockResponse(401, text="nope")
    )
    assert client.ping() is False