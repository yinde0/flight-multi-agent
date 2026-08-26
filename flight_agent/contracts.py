from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DocumentMetadata(BaseModel):
    """Trusted metadata supplied by the upload boundary."""

    model_config = ConfigDict(extra="forbid")

    trip_id: str = Field(pattern=r"^trip-[a-z0-9-]+$")
    traveler_ref: str = Field(min_length=1, max_length=200)
    fixture_id: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=255)
    media_type: Literal["application/pdf"] = "application/pdf"
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ItineraryLeg(BaseModel):
    """One canonical booked flight leg."""

    model_config = ConfigDict(extra="forbid")

    leg_id: str = Field(pattern=r"^leg-[a-z0-9-]+$")
    marketing_carrier: str = Field(pattern=r"^[A-Z0-9]{2,3}$")
    operating_carrier: str | None = Field(
        default=None, pattern=r"^[A-Z0-9]{2,3}$"
    )
    flight_number: str = Field(pattern=r"^[A-Z0-9]{2,3}[0-9]{1,4}$")
    origin: str = Field(pattern=r"^[A-Z]{3}$")
    destination: str = Field(pattern=r"^[A-Z]{3}$")
    scheduled_departure_at: str
    scheduled_arrival_at: str
    minimum_connection_minutes: int | None = Field(default=None, ge=0)


class CanonicalItinerary(BaseModel):
    """Typed form of travel_eval/schemas/itinerary.schema.json."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    trip_id: str = Field(pattern=r"^trip-[a-z0-9-]+$")
    traveler_ref: str = Field(min_length=1, max_length=200)
    confirmation_codes: list[str] = Field(min_length=1)
    legs: list[ItineraryLeg] = Field(min_length=1)


class ParseOutcome(BaseModel):
    """Application result carried inside the A2A artifact."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["parsed", "review_required"]
    document: DocumentMetadata
    itinerary: CanonicalItinerary | None = None
    review: dict[str, Any] | None = None
    orchestration: dict[str, Any]
