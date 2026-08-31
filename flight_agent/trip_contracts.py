from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from flight_agent.contracts import CanonicalItinerary


E164_PATTERN = r"^\+[1-9][0-9]{7,14}$"


def validate_sms_notification_input(
    phone_e164: str | None,
    sms_consent: bool,
) -> str | None:
    cleaned = phone_e164.strip() if phone_e164 else None
    if sms_consent and not cleaned:
        raise ValueError("A mobile number is required for SMS notifications")
    if cleaned and not sms_consent:
        raise ValueError("SMS consent is required when a mobile number is supplied")
    if cleaned:
        import re

        if re.fullmatch(E164_PATTERN, cleaned) is None:
            raise ValueError("Mobile number must use international E.164 format")
    return cleaned


class SmsNotificationPreference(BaseModel):
    """Consent-bearing contact stored separately from itinerary content."""

    model_config = ConfigDict(extra="forbid")

    channel: Literal["sms"] = "sms"
    phone_e164: str = Field(pattern=E164_PATTERN)
    consent_granted_at: str


class NotificationRecipient(BaseModel):
    """Private internal lookup result; never included in public trip views."""

    model_config = ConfigDict(extra="forbid")

    trip_id: str = Field(pattern=r"^trip-[a-z0-9-]+$")
    recipient_ref: str = Field(pattern=r"^traveler:trip-[a-z0-9-]+$")
    channel: Literal["sms"] = "sms"
    phone_e164: str = Field(pattern=E164_PATTERN)
    consent_granted_at: str


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
    trace_headers: dict[str, str] = Field(default_factory=dict)

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
    trip_id: str | None = Field(default=None, pattern=r"^trip-[a-z0-9-]+$")


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
    notification_error_code: str | None = None
    notification_remediation: str | None = None
    search_status: str | None = None
    notification_id: str | None = None
    notification_message: str | None = Field(default=None, max_length=300)
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
