"""Pydantic models mirroring the ingestion API's shapes.

These mirror the backend serializers in ``events/ingestion_serializers.py`` and
the model enums in ``events/models.py``. The extractor only ever *reads*
``DueSource`` / ``IngestionRun`` and *writes* ``ObservationSubmit``. We never
send ``status`` — the backend forces observations to ``pending``.

Field max-lengths match the backend model fields so a pydantic
``ValidationError`` surfaces an over-long value *before* it reaches the API
(rather than as a 400 from the serializer).

Geometry note: the backend stores ``location`` as a PostGIS ``Point`` built
from ``Point(lon, lat)`` — i.e. ``(longitude, latitude)`` order. We send
``latitude`` and ``longitude`` as two separate floats (write-only on the
serializer) and let the backend assemble the point.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --- Enums (values must match the backend TextChoices exactly) ---


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AttendanceMode(str, Enum):
    PHYSICAL = "physical"
    ONLINE = "online"
    HYBRID = "hybrid"


class EventType(str, Enum):
    MEETUP = "meetup"
    CONFERENCE = "conference"
    WORKSHOP = "workshop"
    SOCIAL = "social"
    OTHER = "other"


# --- Read models (what the backend returns) ---


class OrganizationMini(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    name: str
    slug: str


class DueSource(BaseModel):
    """A source eligible for extraction (read-only work queue item)."""

    model_config = ConfigDict(extra="ignore")
    id: int
    organization: OrganizationMini
    url: str
    platform: str | None = None
    fetch_interval_minutes: int = 0
    last_fetched_at: datetime | None = None
    next_due_at: datetime | None = None


class IngestionRun(BaseModel):
    """A run created/finished by the extractor."""

    model_config = ConfigDict(extra="ignore")
    id: int
    source: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: RunStatus = RunStatus.PENDING
    events_found: int = 0
    events_promoted: int = 0
    error_message: str = ""
    created_at: datetime | None = None


# --- Write model (what we submit) ---


class ObservationSubmit(BaseModel):
    """One observation to submit via ``POST /observations/bulk/``.

    ``source`` is required; ``run`` is optional (the runner sets it from the
    run it opened). ``latitude``/``longitude`` are separate floats; the backend
    builds ``Point(lon, lat)``. We never set ``status`` (backend forces
    ``pending``).
    """

    model_config = ConfigDict(extra="forbid")

    source: int
    run: int | None = None
    title: str = Field(..., min_length=1, max_length=200)
    starts_at: datetime
    ends_at: datetime | None = None
    description: str | None = None
    url: str | None = Field(None, max_length=200)
    platform: str | None = Field(None, max_length=80)
    attendance_mode: AttendanceMode | None = None
    event_type: EventType | None = None
    venue_name: str | None = Field(None, max_length=200)
    venue_address: str | None = Field(None, max_length=255)
    venue_city: str | None = Field(None, max_length=100)
    latitude: float | None = None
    longitude: float | None = None
    raw_payload: dict[str, Any] | None = None

    def to_api(self) -> dict[str, Any]:
        """Emit the exact field set the submit serializer accepts.

        Drops ``None`` values (the serializer treats them as optional) and
        omits ``status`` (the backend forces ``pending``). The ``run`` is only
        included when set. ``latitude``/``longitude`` are kept as floats so the
        backend can build the point.
        """
        payload: dict[str, Any] = {"source": self.source}
        if self.run is not None:
            payload["run"] = self.run
        payload["title"] = self.title
        payload["starts_at"] = self.starts_at.isoformat()
        for field in (
            "ends_at",
            "description",
            "url",
            "platform",
            "attendance_mode",
            "event_type",
            "venue_name",
            "venue_address",
            "venue_city",
        ):
            value = getattr(self, field)
            if value is None:
                continue
            if isinstance(value, Enum):
                value = value.value
            if isinstance(value, datetime):
                value = value.isoformat()
            payload[field] = value
        if self.latitude is not None:
            payload["latitude"] = self.latitude
        if self.longitude is not None:
            payload["longitude"] = self.longitude
        if self.raw_payload is not None:
            payload["raw_payload"] = self.raw_payload
        return payload