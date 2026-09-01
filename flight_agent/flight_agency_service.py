from __future__ import annotations

import hashlib
import hmac
import os
import threading

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException

from flight_agent.flight_agency_contracts import (
    AgencyFlightCollection,
    AgencyFlightDetails,
    AgencyFlightEvent,
    AgencyFlightMutation,
    AgencyFlightSeed,
    AgencyFlightSeedBatch,
    AgencyFlightView,
)
from flight_agent.monitoring_contracts import ProviderFlightObservation
from flight_agent.telemetry import install_telemetry_routes
from travel_eval.clock import parse_timestamp


class FlightAgencyConflictError(RuntimeError):
    """A flight identity already exists with a different immutable schedule."""


class FlightAgencyNotFoundError(RuntimeError):
    """The requested simulated flight does not exist."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _add_minutes(value: str, minutes: int) -> str:
    return _format_timestamp(parse_timestamp(value) + timedelta(minutes=minutes))


class InMemoryFlightAgencyStore:
    """Thread-safe, resettable state for a local flight-operations sandbox."""

    def __init__(self, *, clock: Callable[[], datetime] = _now_utc) -> None:
        self._clock = clock
        self._records: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(flight_iata: str, flight_date: str) -> tuple[str, str]:
        return flight_iata.upper(), flight_date

    @staticmethod
    def _observation_id(flight_iata: str, flight_date: str, revision: int) -> str:
        identity = f"{flight_iata}:{flight_date}:{revision}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return f"obs-agency-{digest}"

    def _next_changed_at(self, previous: str | None = None) -> str:
        current = self._clock().astimezone(timezone.utc)
        if previous is not None:
            previous_time = parse_timestamp(previous)
            if current <= previous_time:
                current = previous_time + timedelta(microseconds=1)
        return _format_timestamp(current)

    @staticmethod
    def _immutable(seed: AgencyFlightSeed) -> tuple[str, ...]:
        return (
            seed.flight_iata,
            seed.flight_date,
            seed.origin,
            seed.destination,
            seed.scheduled_departure_at,
            seed.scheduled_arrival_at,
        )

    def seed(self, seed: AgencyFlightSeed) -> AgencyFlightView:
        key = self._key(seed.flight_iata, seed.flight_date)
        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                existing_seed = AgencyFlightSeed.model_validate(existing["seed"])
                if self._immutable(existing_seed) != self._immutable(seed):
                    raise FlightAgencyConflictError(
                        "Flight identity already has a different schedule"
                    )
                return self._view(existing)

            changed_at = self._next_changed_at()
            record: dict[str, Any] = {
                "seed": seed.model_dump(mode="json"),
                "status": "scheduled",
                "departure_delay_minutes": 0,
                "arrival_delay_minutes": 0,
                "departure_terminal": seed.departure_terminal,
                "departure_gate": seed.departure_gate,
                "arrival_terminal": seed.arrival_terminal,
                "arrival_gate": seed.arrival_gate,
                "revision": 1,
                "updated_at": changed_at,
                "history": [
                    AgencyFlightEvent(
                        revision=1,
                        changed_at=changed_at,
                        changed_fields=["flight_created"],
                        note="Created from booked itinerary",
                    ).model_dump(mode="json")
                ],
            }
            self._records[key] = record
            return self._view(record)

    def seed_batch(self, batch: AgencyFlightSeedBatch) -> AgencyFlightCollection:
        flights = []
        for item in batch.flights:
            view = self.seed(item)
            if batch.reset_existing:
                view = self.reset(item.flight_iata, item.flight_date)
            flights.append(view)
        return AgencyFlightCollection(flights=flights)

    def list(self) -> AgencyFlightCollection:
        with self._lock:
            records = sorted(
                self._records.values(),
                key=lambda item: (
                    str(item["seed"]["scheduled_departure_at"]),
                    str(item["seed"]["flight_iata"]),
                ),
            )
            return AgencyFlightCollection(flights=[self._view(item) for item in records])

    def get(self, flight_iata: str, flight_date: str) -> AgencyFlightDetails:
        with self._lock:
            record = self._records.get(self._key(flight_iata, flight_date))
            if record is None:
                raise FlightAgencyNotFoundError("Simulated flight not found")
            view = self._view(record)
            return AgencyFlightDetails(
                **view.model_dump(mode="json"),
                history=[AgencyFlightEvent.model_validate(item) for item in record["history"]],
            )

    def mutate(
        self,
        flight_iata: str,
        flight_date: str,
        mutation: AgencyFlightMutation,
    ) -> AgencyFlightDetails:
        with self._lock:
            record = self._records.get(self._key(flight_iata, flight_date))
            if record is None:
                raise FlightAgencyNotFoundError("Simulated flight not found")
            supplied = mutation.model_dump(exclude_none=True, exclude={"note"})
            changed_fields = [
                field for field, value in supplied.items() if record.get(field) != value
            ]
            if changed_fields:
                for field in changed_fields:
                    record[field] = supplied[field]
                record["revision"] = int(record["revision"]) + 1
                record["updated_at"] = self._next_changed_at(str(record["updated_at"]))
                record["history"].append(
                    AgencyFlightEvent(
                        revision=record["revision"],
                        changed_at=record["updated_at"],
                        changed_fields=changed_fields,
                        note=mutation.note,
                    ).model_dump(mode="json")
                )
            return self.get(flight_iata, flight_date)

    def reset(self, flight_iata: str, flight_date: str) -> AgencyFlightDetails:
        with self._lock:
            record = self._records.get(self._key(flight_iata, flight_date))
            if record is None:
                raise FlightAgencyNotFoundError("Simulated flight not found")
            seed = AgencyFlightSeed.model_validate(record["seed"])
            return self.mutate(
                flight_iata,
                flight_date,
                AgencyFlightMutation(
                    status="scheduled",
                    departure_delay_minutes=0,
                    arrival_delay_minutes=0,
                    departure_terminal=seed.departure_terminal or "1",
                    departure_gate=seed.departure_gate or "A10",
                    arrival_terminal=seed.arrival_terminal or "1",
                    arrival_gate=seed.arrival_gate or "B10",
                    note="Restored to the booked schedule",
                ),
            )

    def reset_all(self) -> None:
        with self._lock:
            self._records.clear()

    def observation(
        self, flight_iata: str, flight_date: str
    ) -> ProviderFlightObservation:
        details = self.get(flight_iata, flight_date)
        observed_at = _format_timestamp(self._clock())
        freshness = max(
            0,
            int(
                (
                    parse_timestamp(observed_at) - parse_timestamp(details.updated_at)
                ).total_seconds()
            ),
        )
        return ProviderFlightObservation(
            observation_id=details.observation_id,
            observed_at=observed_at,
            source="flight-agency-sandbox",
            source_event_time=details.updated_at,
            status=details.status,
            departure={
                "scheduled_at": details.scheduled_departure_at,
                "estimated_at": details.estimated_departure_at,
                "actual_at": None,
                "terminal": details.departure_terminal,
                "gate": details.departure_gate,
            },
            arrival={
                "scheduled_at": details.scheduled_arrival_at,
                "estimated_at": details.estimated_arrival_at,
                "actual_at": None,
                "terminal": details.arrival_terminal,
                "gate": details.arrival_gate,
            },
            departure_airport=details.origin,
            destination_airport=details.destination,
            data_freshness_seconds=freshness,
            confidence=0.99,
        )

    def _view(self, record: dict[str, Any]) -> AgencyFlightView:
        seed = AgencyFlightSeed.model_validate(record["seed"])
        revision = int(record["revision"])
        return AgencyFlightView(
            flight_iata=seed.flight_iata,
            flight_date=seed.flight_date,
            origin=seed.origin,
            destination=seed.destination,
            status=record["status"],
            scheduled_departure_at=seed.scheduled_departure_at,
            estimated_departure_at=_add_minutes(
                seed.scheduled_departure_at, int(record["departure_delay_minutes"])
            ),
            scheduled_arrival_at=seed.scheduled_arrival_at,
            estimated_arrival_at=_add_minutes(
                seed.scheduled_arrival_at, int(record["arrival_delay_minutes"])
            ),
            departure_delay_minutes=record["departure_delay_minutes"],
            arrival_delay_minutes=record["arrival_delay_minutes"],
            departure_terminal=record["departure_terminal"],
            departure_gate=record["departure_gate"],
            arrival_terminal=record["arrival_terminal"],
            arrival_gate=record["arrival_gate"],
            revision=revision,
            observation_id=self._observation_id(
                seed.flight_iata, seed.flight_date, revision
            ),
            updated_at=record["updated_at"],
        )


def create_flight_agency_app(
    *,
    store: InMemoryFlightAgencyStore | None = None,
    control_token: str | None = None,
) -> FastAPI:
    resolved_store = store or InMemoryFlightAgencyStore()
    resolved_token = (
        control_token
        if control_token is not None
        else os.getenv("FLIGHT_AGENCY_CONTROL_TOKEN", "")
    )
    app = FastAPI(title="Local Flight Agency Sandbox", version="0.1.0")
    app.state.store = resolved_store
    app.state.control_token = resolved_token
    install_telemetry_routes(app, service_name="flight-agency-simulator")

    def authorize(value: str | None) -> None:
        if not app.state.control_token:
            raise HTTPException(status_code=503, detail="Flight agency control is disabled")
        if value is None or not hmac.compare_digest(value, app.state.control_token):
            raise HTTPException(status_code=403, detail="Invalid flight agency control token")

    def not_found(error: FlightAgencyNotFoundError) -> HTTPException:
        return HTTPException(status_code=404, detail=str(error))

    @app.get("/health/live", tags=["health"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "control_enabled": bool(app.state.control_token),
            "flight_count": len(resolved_store.list().flights),
        }

    @app.get("/v1/flights", response_model=AgencyFlightCollection)
    async def list_flights(
        token: str | None = Header(default=None, alias="X-Flight-Agency-Token"),
    ) -> AgencyFlightCollection:
        authorize(token)
        return resolved_store.list()

    @app.post("/v1/flights", response_model=AgencyFlightCollection)
    async def seed_flights(
        request: AgencyFlightSeedBatch,
        token: str | None = Header(default=None, alias="X-Flight-Agency-Token"),
    ) -> AgencyFlightCollection:
        authorize(token)
        try:
            return resolved_store.seed_batch(request)
        except FlightAgencyConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get(
        "/v1/flights/{flight_iata}/{flight_date}",
        response_model=AgencyFlightDetails,
    )
    async def get_flight(
        flight_iata: str,
        flight_date: str,
        token: str | None = Header(default=None, alias="X-Flight-Agency-Token"),
    ) -> AgencyFlightDetails:
        authorize(token)
        try:
            return resolved_store.get(flight_iata.upper(), flight_date)
        except FlightAgencyNotFoundError as error:
            raise not_found(error) from error

    @app.patch(
        "/v1/flights/{flight_iata}/{flight_date}",
        response_model=AgencyFlightDetails,
    )
    async def change_flight(
        flight_iata: str,
        flight_date: str,
        request: AgencyFlightMutation,
        token: str | None = Header(default=None, alias="X-Flight-Agency-Token"),
    ) -> AgencyFlightDetails:
        authorize(token)
        try:
            return resolved_store.mutate(flight_iata.upper(), flight_date, request)
        except FlightAgencyNotFoundError as error:
            raise not_found(error) from error

    @app.post(
        "/v1/flights/{flight_iata}/{flight_date}/reset",
        response_model=AgencyFlightDetails,
    )
    async def reset_flight(
        flight_iata: str,
        flight_date: str,
        token: str | None = Header(default=None, alias="X-Flight-Agency-Token"),
    ) -> AgencyFlightDetails:
        authorize(token)
        try:
            return resolved_store.reset(flight_iata.upper(), flight_date)
        except FlightAgencyNotFoundError as error:
            raise not_found(error) from error

    @app.delete("/v1/flights", status_code=204)
    async def reset_all(
        token: str | None = Header(default=None, alias="X-Flight-Agency-Token"),
    ) -> None:
        authorize(token)
        resolved_store.reset_all()

    @app.get(
        "/v1/provider/flights/{flight_iata}/{flight_date}",
        response_model=ProviderFlightObservation,
    )
    async def provider_observation(
        flight_iata: str,
        flight_date: str,
        token: str | None = Header(default=None, alias="X-Flight-Agency-Token"),
    ) -> ProviderFlightObservation:
        authorize(token)
        try:
            return resolved_store.observation(flight_iata.upper(), flight_date)
        except FlightAgencyNotFoundError as error:
            raise not_found(error) from error

    return app


app = create_flight_agency_app()
