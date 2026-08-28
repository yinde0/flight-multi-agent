from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from travel_eval.clock import parse_timestamp


FLIGHT_IATA_PATTERN = r"^[A-Z0-9]{2,3}[0-9]{1,4}$"
FLIGHT_DATE_PATTERN = r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}$"
AIRPORT_PATTERN = r"^[A-Z]{3}$"

AgencyFlightStatus = Literal[
    "scheduled", "active", "landed", "cancelled", "diverted"
]


class AgencyFlightSeed(BaseModel):
    """Immutable schedule used to create a flight in the local agency sandbox."""

    model_config = ConfigDict(extra="forbid")

    flight_iata: str = Field(pattern=FLIGHT_IATA_PATTERN)
    flight_date: str = Field(pattern=FLIGHT_DATE_PATTERN)
    origin: str = Field(pattern=AIRPORT_PATTERN)
    destination: str = Field(pattern=AIRPORT_PATTERN)
    scheduled_departure_at: str
    scheduled_arrival_at: str
    departure_terminal: str | None = Field(default=None, max_length=12)
    departure_gate: str | None = Field(default=None, max_length=12)
    arrival_terminal: str | None = Field(default=None, max_length=12)
    arrival_gate: str | None = Field(default=None, max_length=12)

    @model_validator(mode="after")
    def validate_schedule(self) -> "AgencyFlightSeed":
        departure = parse_timestamp(self.scheduled_departure_at)
        arrival = parse_timestamp(self.scheduled_arrival_at)
        if departure.date() != date.fromisoformat(self.flight_date):
            raise ValueError("flight_date must match scheduled departure date")
        if arrival <= departure:
            raise ValueError("scheduled arrival must be after scheduled departure")
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        return self


class AgencyFlightSeedBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flights: list[AgencyFlightSeed] = Field(min_length=1, max_length=20)
    reset_existing: bool = False


class AgencyFlightMutation(BaseModel):
    """Operator-authored change; omitted fields keep their current value."""

    model_config = ConfigDict(extra="forbid")

    status: AgencyFlightStatus | None = None
    departure_delay_minutes: int | None = Field(default=None, ge=0, le=720)
    arrival_delay_minutes: int | None = Field(default=None, ge=0, le=720)
    departure_terminal: str | None = Field(default=None, min_length=1, max_length=12)
    departure_gate: str | None = Field(default=None, min_length=1, max_length=12)
    arrival_terminal: str | None = Field(default=None, min_length=1, max_length=12)
    arrival_gate: str | None = Field(default=None, min_length=1, max_length=12)
    note: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def require_change(self) -> "AgencyFlightMutation":
        fields = self.model_dump(exclude_none=True, exclude={"note"})
        if not fields:
            raise ValueError("At least one flight field must be changed")
        return self


class AgencyFlightEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    changed_at: str
    changed_fields: list[str] = Field(min_length=1)
    note: str | None = None


class AgencyFlightView(BaseModel):
    """PII-free operator view of one simulated flight."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    flight_iata: str = Field(pattern=FLIGHT_IATA_PATTERN)
    flight_date: str = Field(pattern=FLIGHT_DATE_PATTERN)
    origin: str = Field(pattern=AIRPORT_PATTERN)
    destination: str = Field(pattern=AIRPORT_PATTERN)
    status: AgencyFlightStatus
    scheduled_departure_at: str
    estimated_departure_at: str
    scheduled_arrival_at: str
    estimated_arrival_at: str
    departure_delay_minutes: int = Field(ge=0, le=720)
    arrival_delay_minutes: int = Field(ge=0, le=720)
    departure_terminal: str | None = None
    departure_gate: str | None = None
    arrival_terminal: str | None = None
    arrival_gate: str | None = None
    revision: int = Field(ge=1)
    observation_id: str = Field(min_length=1)
    updated_at: str


class AgencyFlightDetails(AgencyFlightView):
    history: list[AgencyFlightEvent] = Field(default_factory=list)


class AgencyFlightCollection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flights: list[AgencyFlightView] = Field(default_factory=list)


class AgencyDemoStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    flight_count: int = Field(ge=0)
    provider: Literal["flight-agency-sandbox"] = "flight-agency-sandbox"


class AgencyTripSyncOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trip_id: str = Field(pattern=r"^trip-[a-z0-9-]+$")
    flights: list[AgencyFlightView] = Field(default_factory=list)
