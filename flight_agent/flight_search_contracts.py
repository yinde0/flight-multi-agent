from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchEvalApproval(BaseModel):
    """The only Eval verdict allowed to cross the search boundary."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    verdict: Literal["NOTIFY_AND_SEARCH"]
    policy_version: str = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)
    decided_at: str


class FlightSearchCommand(BaseModel):
    """Read-only search request carrying proof of Eval authorization."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    search_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    trip_id: str = Field(min_length=1)
    leg_id: str = Field(min_length=1)
    original_flight_iata: str = Field(pattern=r"^[A-Z0-9]{2,3}[0-9]{1,4}$")
    origin: str = Field(pattern=r"^[A-Z]{3}$")
    destination: str = Field(pattern=r"^[A-Z]{3}$")
    departure_date: str = Field(pattern=r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}$")
    earliest_departure_at: str
    latest_departure_at: str
    maximum_stops: int = Field(default=1, ge=0, le=2)
    minimum_connection_minutes: int = Field(default=45, ge=0, le=240)
    passenger_count: int = Field(default=1, ge=1, le=9)
    cabin_class: Literal["economy", "premium_economy", "business", "first"] = (
        "economy"
    )
    approval: SearchEvalApproval


class FlightSearchSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flight_iata: str = Field(pattern=r"^[A-Z0-9]{2,3}[0-9]{1,4}$")
    origin: str = Field(pattern=r"^[A-Z]{3}$")
    destination: str = Field(pattern=r"^[A-Z]{3}$")
    departure_at: str
    arrival_at: str


class FlightOfferPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: str = Field(pattern=r"^[0-9]+(?:\.[0-9]{1,6})?$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class ProviderFlightOption(BaseModel):
    """Schedule or priced provider offer normalized behind the MCP boundary."""

    model_config = ConfigDict(extra="forbid")

    option_id: str = Field(min_length=1)
    segments: list[FlightSearchSegment] = Field(min_length=1, max_length=3)
    price: FlightOfferPrice | None = None
    offer_expires_at: str | None = None
    owner_name: str | None = None
    owner_iata_code: str | None = Field(default=None, pattern=r"^[A-Z0-9]{2,3}$")
    passenger_count: int = Field(default=1, ge=1, le=9)
    live_mode: bool | None = None
    availability_status: Literal[
        "schedule_only", "synthetic_replay", "provider_test_offer", "live_offer"
    ] = "schedule_only"


class FlightSearchToolResult(BaseModel):
    """Provider-neutral output returned by the search MCP tool."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    search_id: str
    decision_id: str
    idempotency_key: str
    provider: str
    source_scope: Literal[
        "synthetic_replay", "schedule_only", "provider_test_offers", "live_offers"
    ]
    searched_at: str
    options: list[ProviderFlightOption]
    availability_verified: bool = False
    booking_guaranteed: Literal[False] = False
    booking_authorized: Literal[False] = False


class RankedFlightOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1)
    option_id: str
    segments: list[FlightSearchSegment]
    stops: int = Field(ge=0)
    departure_at: str
    arrival_at: str
    price: FlightOfferPrice | None = None
    offer_expires_at: str | None = None
    owner_name: str | None = None
    owner_iata_code: str | None = None
    passenger_count: int = Field(default=1, ge=1, le=9)
    live_mode: bool | None = None
    availability_status: Literal[
        "schedule_only", "synthetic_replay", "provider_test_offer", "live_offer"
    ] = "schedule_only"


class FlightSearchActionRecord(BaseModel):
    """Durable, non-booking audit result of an authorized search."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    search_id: str
    candidate_id: str
    decision_id: str
    trip_id: str
    leg_id: str
    verdict: Literal["NOTIFY", "NOTIFY_AND_SEARCH"]
    status: Literal["completed", "no_options", "failed", "rejected"]
    idempotency_key: str
    provider: str | None = None
    source_scope: Literal[
        "synthetic_replay", "schedule_only", "provider_test_offers", "live_offers"
    ] | None = None
    alternatives: list[RankedFlightOption] = Field(default_factory=list)
    rejection_summary: dict[str, int] = Field(default_factory=dict)
    availability_verified: bool = False
    booking_guaranteed: Literal[False] = False
    booking_authorized: Literal[False] = False
    recorded_at: str
    error_code: str | None = None
