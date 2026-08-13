"""Tests for the models: enums, validation, and to_api() payload shape."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from terminschleuder_extractor.models import (
    AttendanceMode,
    EventType,
    ObservationSubmit,
    RunStatus,
)


def _valid(**overrides):
    base = dict(
        source=42,
        title="PyGraten",
        starts_at=datetime(2025, 9, 15, 19, 0, 0),
    )
    base.update(overrides)
    return ObservationSubmit(**base)


def test_enum_values_match_backend():
    assert {e.value for e in RunStatus} == {"pending", "running", "succeeded", "failed"}
    assert {e.value for e in AttendanceMode} == {"physical", "online", "hybrid"}
    assert {e.value for e in EventType} == {"meetup", "conference", "workshop", "social", "other"}


def test_required_fields():
    with pytest.raises(ValidationError):
        ObservationSubmit(source=42)  # missing title + starts_at


def test_title_max_length():
    with pytest.raises(ValidationError):
        _valid(title="x" * 201)


def test_to_api_minimal():
    obs = _valid()
    payload = obs.to_api()
    assert payload["source"] == 42
    assert payload["title"] == "PyGraten"
    assert payload["starts_at"] == "2025-09-15T19:00:00"
    # optionals omitted, status never sent, run omitted when None.
    assert "run" not in payload
    assert "status" not in payload
    assert "description" not in payload
    assert "latitude" not in payload


def test_to_api_includes_run_and_optionals():
    obs = _valid(
        run=99,
        ends_at=datetime(2025, 9, 15, 22, 0, 0),
        attendance_mode=AttendanceMode.ONLINE,
        event_type=EventType.MEETUP,
        latitude=52.52,
        longitude=13.405,
        venue_city="Berlin",
        raw_payload={"foo": "bar"},
    )
    payload = obs.to_api()
    assert payload["run"] == 99
    assert payload["ends_at"] == "2025-09-15T22:00:00"
    assert payload["attendance_mode"] == "online"
    assert payload["event_type"] == "meetup"
    assert payload["latitude"] == 52.52
    assert payload["longitude"] == 13.405
    assert payload["venue_city"] == "Berlin"
    assert payload["raw_payload"] == {"foo": "bar"}


def test_to_api_serializes_enum_values():
    obs = _valid(event_type=EventType.CONFERENCE)
    assert obs.to_api()["event_type"] == "conference"


def test_extra_field_forbidden():
    with pytest.raises(ValidationError):
        ObservationSubmit(
            source=42, title="x", starts_at=datetime(2025, 1, 1), bogus=1
        )