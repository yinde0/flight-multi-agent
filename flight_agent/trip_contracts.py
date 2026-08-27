from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from flight_agent.contracts import CanonicalItinerary


class DocumentObjectRef(BaseModel):
    """Opaque S3 reference; no provider credentials or public URL."""

    model_config = ConfigDict(extra="forbid")

    bucket: str = Field(min_length=1)
    key: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    etag: str | None = None


class TripActivationOutcome(BaseModel):
    """Result of parsing, storing, and registering one uploaded itinerary."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    status: Literal["activated", "already_active", "review_required"]
    trip_id: str = Field(pattern=r"^trip-[a-z0-9-]+$")
    trip_status: Literal["active", "review_required"]
    parse_status: Literal["parsed", "review_required"]
    document: DocumentObjectRef
    itinerary: CanonicalItinerary | None = None
    review: dict[str, Any] | None = None
    active_leg_count: int = Field(ge=0)
    next_poll_at: str | None = None
    idempotent_replay: bool = False


class ScheduledLeg(BaseModel):
    """A Postgres-owned monitoring lease for one due flight leg."""

    model_config = ConfigDict(extra="forbid")

    trip_id: str = Field(pattern=r"^trip-[a-z0-9-]+$")
    leg_id: str = Field(pattern=r"^leg-[a-z0-9-]+$")
    flight_iata: str = Field(pattern=r"^[A-Z0-9]{2,3}[0-9]{1,4}$")
    flight_date: str = Field(pattern=r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}$")
    scheduled_departure_at: str
    scheduled_arrival_at: str
    due_at: str
    replay_key: str = Field(min_length=1)

    @property
    def poll_key(self) -> str:
        return f"{self.trip_id}:{self.leg_id}:{self.due_at}"


class StoredLegView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leg_id: str
    flight_iata: str
    origin: str
    destination: str
    monitoring_status: Literal["active", "completed"]
    next_poll_at: str | None = None
    last_poll_at: str | None = None
    poll_count: int = Field(ge=0)
    last_poll_status: str | None = None


class StoredTripView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    trip_id: str
    traveler_ref: str
    status: Literal["active", "review_required", "completed"]
    document: DocumentObjectRef
    itinerary: CanonicalItinerary | None = None
    review: dict[str, Any] | None = None
    legs: list[StoredLegView] = Field(default_factory=list)
    created_at: str
    updated_at: str


class SchedulerTickRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    now: str
    maximum_legs: int = Field(default=20, ge=1, le=100)


class SchedulerPollResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trip_id: str
    leg_id: str
    poll_key: str
    status: Literal["completed", "failed"]
    monitoring_status: str | None = None
    category: str | None = None
    verdict: str | None = None
    notification_status: str | None = None
    search_status: str | None = None
    notification_id: str | None = None
    search_id: str | None = None
    error_code: str | None = None


class DocumentStorageStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trip_id: str
    stored: bool
    document: DocumentObjectRef


class SchedulerTickOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    requested_at: str
    claimed_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    results: list[SchedulerPollResult] = Field(default_factory=list)
