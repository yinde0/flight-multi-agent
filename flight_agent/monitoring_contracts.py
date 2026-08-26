from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FlightMovement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_at: str
    estimated_at: str | None = None
    actual_at: str | None = None
    terminal: str | None = None
    gate: str | None = None


class NeutralWeather(BaseModel):
    model_config = ConfigDict(extra="forbid")

    airport: str = Field(pattern=r"^[A-Z]{3}$")
    valid_at: str
    risk_level: Literal["none", "low", "moderate", "high", "severe"] = "none"
    alerts: list[str] = Field(default_factory=list)


class ProviderFlightObservation(BaseModel):
    """Normalized output returned by the flight-status MCP tool."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    observation_id: str = Field(min_length=1)
    observed_at: str
    source: str = Field(min_length=1)
    source_event_time: str
    status: Literal[
        "unknown", "scheduled", "active", "landed", "cancelled", "diverted"
    ]
    departure: FlightMovement
    arrival: FlightMovement
    weather: NeutralWeather
    data_freshness_seconds: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)


class MonitoringPollRequest(BaseModel):
    """Trusted orchestration request sent to the Monitoring Agent over A2A."""

    model_config = ConfigDict(extra="forbid")

    trip_id: str = Field(pattern=r"^trip-[a-z0-9-]+$")
    leg_id: str = Field(pattern=r"^leg-[a-z0-9-]+$")
    flight_iata: str = Field(pattern=r"^[A-Z0-9]{2,3}[0-9]{1,4}$")
    flight_date: str = Field(pattern=r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}$")
    replay_key: str | None = Field(default=None, min_length=1, max_length=200)


class MonitoringPollOutcome(BaseModel):
    """One stateful monitoring poll and any downstream Eval Agent result."""

    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "baseline_stored",
        "unchanged",
        "stale_observation",
        "candidate_evaluated",
        "evaluation_pending",
        "poll_failed",
    ]
    request: MonitoringPollRequest
    observation: dict[str, Any] | None = None
    candidate: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    confirmed_event: dict[str, Any] | None = None
    error_code: str | None = None
    orchestration: dict[str, Any]
