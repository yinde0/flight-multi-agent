from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from fastapi.testclient import TestClient

from flight_agent.api import create_api_app
from flight_agent.contracts import CanonicalItinerary
from flight_agent.flight_agency_contracts import (
    AgencyFlightCollection,
    AgencyFlightDetails,
    AgencyFlightMutation,
    AgencyFlightView,
)
from flight_agent.flight_agency_service import (
    InMemoryFlightAgencyStore,
    create_flight_agency_app,
)
from flight_agent.flight_status import FlightAgencyStatusProvider
from flight_agent.trip_contracts import (
    DocumentObjectRef,
    SchedulerTickOutcome,
    StoredLegView,
    StoredTripView,
)


TOKEN = "local-flight-agency-test"
HEADERS = {"X-Flight-Agency-Token": TOKEN}


def seed_payload() -> dict[str, Any]:
    return {
        "flight_iata": "NB204",
        "flight_date": "2026-09-15",
        "origin": "LHR",
        "destination": "AMS",
        "scheduled_departure_at": "2026-09-15T08:20:00Z",
        "scheduled_arrival_at": "2026-09-15T09:35:00Z",
        "departure_terminal": "1",
        "departure_gate": "A10",
        "arrival_terminal": "1",
        "arrival_gate": "B10",
    }


def test_flight_agency_requires_control_token_and_tracks_operator_revisions() -> None:
    moments = iter(
        [
            datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 28, 10, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 28, 10, 0, 2, tzinfo=timezone.utc),
            datetime(2026, 8, 28, 10, 0, 3, tzinfo=timezone.utc),
            datetime(2026, 8, 28, 10, 0, 4, tzinfo=timezone.utc),
            datetime(2026, 8, 28, 10, 0, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 28, 10, 0, 6, tzinfo=timezone.utc),
        ]
    )
    store = InMemoryFlightAgencyStore(clock=lambda: next(moments))
    client = TestClient(create_flight_agency_app(store=store, control_token=TOKEN))

    assert client.get("/v1/flights").status_code == 403
    created = client.post(
        "/v1/flights", headers=HEADERS, json={"flights": [seed_payload()]}
    )
    assert created.status_code == 200
    baseline = created.json()["flights"][0]
    assert baseline["revision"] == 1
    assert baseline["departure_delay_minutes"] == 0

    changed = client.patch(
        "/v1/flights/NB204/2026-09-15",
        headers=HEADERS,
        json={"departure_gate": "C14", "note": "Manual gate move"},
    )
    assert changed.status_code == 200
    assert changed.json()["revision"] == 2
    assert changed.json()["departure_gate"] == "C14"
    assert changed.json()["history"][-1]["changed_fields"] == ["departure_gate"]

    duplicate = client.patch(
        "/v1/flights/NB204/2026-09-15",
        headers=HEADERS,
        json={"departure_gate": "C14"},
    )
    assert duplicate.json()["revision"] == 2

    observation = client.get(
        "/v1/provider/flights/NB204/2026-09-15", headers=HEADERS
    )
    assert observation.status_code == 200
    assert observation.json()["source"] == "flight-agency-sandbox"
    assert observation.json()["departure"]["gate"] == "C14"

    reset = client.post(
        "/v1/flights/NB204/2026-09-15/reset", headers=HEADERS
    )
    assert reset.json()["revision"] == 3
    assert reset.json()["departure_gate"] == "A10"

    client.patch(
        "/v1/flights/NB204/2026-09-15",
        headers=HEADERS,
        json={"status": "cancelled"},
    ).raise_for_status()
    resynced = client.post(
        "/v1/flights",
        headers=HEADERS,
        json={"flights": [seed_payload()], "reset_existing": True},
    )
    assert resynced.json()["flights"][0]["status"] == "scheduled"


def test_flight_agency_status_provider_normalizes_private_server_response() -> None:
    observation = {
        "schema_version": "1.0.0",
        "observation_id": "obs-agency-test",
        "observed_at": "2026-08-28T10:00:00Z",
        "source": "flight-agency-sandbox",
        "source_event_time": "2026-08-28T09:59:59Z",
        "status": "cancelled",
        "departure": {
            "scheduled_at": "2026-09-15T08:20:00Z",
            "estimated_at": "2026-09-15T08:20:00Z",
            "actual_at": None,
            "terminal": "1",
            "gate": "A10",
        },
        "arrival": {
            "scheduled_at": "2026-09-15T09:35:00Z",
            "estimated_at": "2026-09-15T09:35:00Z",
            "actual_at": None,
            "terminal": "1",
            "gate": "B10",
        },
        "departure_airport": "LHR",
        "destination_airport": "AMS",
        "data_freshness_seconds": 1,
        "confidence": 0.99,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Flight-Agency-Token"] == TOKEN
        assert request.url.path.endswith("/NB204/2026-09-15")
        return httpx.Response(200, request=request, json=observation)

    provider = FlightAgencyStatusProvider(
        base_url="http://agency.test",
        control_token=TOKEN,
        transport=httpx.MockTransport(handler),
    )

    result = provider.get_flight_status(
        flight_iata="NB204", flight_date="2026-09-15"
    )

    assert result.status == "cancelled"
    assert result.departure.gate == "A10"


def stored_trip() -> StoredTripView:
    itinerary = CanonicalItinerary.model_validate(
        {
            "trip_id": "trip-agency-demo",
            "traveler_ref": "traveler-agency-demo",
            "confirmation_codes": ["NX4K7Q"],
            "legs": [
                {
                    "leg_id": "leg-agency-demo-1",
                    "marketing_carrier": "NB",
                    "operating_carrier": "NB",
                    "flight_number": "NB204",
                    "origin": "LHR",
                    "destination": "AMS",
                    "scheduled_departure_at": "2026-09-15T08:20:00Z",
                    "scheduled_arrival_at": "2026-09-15T09:35:00Z",
                }
            ],
        }
    )
    return StoredTripView(
        trip_id="trip-agency-demo",
        traveler_ref="traveler-agency-demo",
        status="active",
        document=DocumentObjectRef(
            bucket="test", key="opaque.pdf", sha256="a" * 64
        ),
        itinerary=itinerary,
        legs=[
            StoredLegView(
                leg_id="leg-agency-demo-1",
                flight_iata="NB204",
                origin="LHR",
                destination="AMS",
                monitoring_status="active",
                next_poll_at="2026-08-28T10:00:00.500000Z",
                poll_count=0,
            )
        ],
        created_at="2026-08-28T10:00:00Z",
        updated_at="2026-08-28T10:00:00Z",
    )


def agency_view() -> AgencyFlightView:
    return AgencyFlightView(
        **seed_payload(),
        status="scheduled",
        estimated_departure_at="2026-09-15T08:20:00Z",
        estimated_arrival_at="2026-09-15T09:35:00Z",
        departure_delay_minutes=0,
        arrival_delay_minutes=0,
        revision=1,
        observation_id="obs-agency-test",
        updated_at="2026-08-28T10:00:00Z",
    )


class FakeTripGateway:
    def __init__(self) -> None:
        self.trip = stored_trip()
        self.tick_requests = []

    async def get_trip(self, trip_id: str) -> StoredTripView:
        assert trip_id == self.trip.trip_id
        return self.trip

    async def tick(self, request):
        self.tick_requests.append(request)
        return SchedulerTickOutcome(
            requested_at=request.now,
            claimed_count=1,
            completed_count=1,
            failed_count=0,
            results=[],
        )


class FakeAgencyGateway:
    def __init__(self) -> None:
        self.view = agency_view()

    async def health(self) -> int:
        return 1

    async def seed_itinerary(self, itinerary) -> AgencyFlightCollection:
        assert itinerary.trip_id == "trip-agency-demo"
        return AgencyFlightCollection(flights=[self.view])

    async def list_flights(self) -> AgencyFlightCollection:
        return AgencyFlightCollection(flights=[self.view])

    async def change_flight(
        self, flight_iata: str, flight_date: str, mutation: AgencyFlightMutation
    ) -> AgencyFlightDetails:
        payload = self.view.model_dump()
        payload["status"] = mutation.status or self.view.status
        return AgencyFlightDetails(**payload, history=[])

    async def reset_flight(self, flight_iata: str, flight_date: str):
        return AgencyFlightDetails(**self.view.model_dump(), history=[])


def test_travel_api_gates_demo_and_runs_only_the_selected_trip() -> None:
    trip_gateway = FakeTripGateway()
    agency_gateway = FakeAgencyGateway()
    disabled = TestClient(
        create_api_app(
            gateway=object(),
            monitoring_gateway=object(),
            trip_gateway=trip_gateway,
            flight_agency_gateway=agency_gateway,
            flight_agency_demo_enabled=False,
        )
    )
    assert disabled.get("/v1/demo/agency/status").status_code == 404

    client = TestClient(
        create_api_app(
            gateway=object(),
            monitoring_gateway=object(),
            trip_gateway=trip_gateway,
            scheduler_control_enabled=True,
            flight_agency_gateway=agency_gateway,
            flight_agency_demo_enabled=True,
        )
    )
    synced = client.post("/v1/demo/agency/trips/trip-agency-demo/sync")
    assert synced.status_code == 200
    assert synced.json()["flights"][0]["flight_iata"] == "NB204"

    checked = client.post("/v1/demo/agency/trips/trip-agency-demo/check")
    assert checked.status_code == 200
    request = trip_gateway.tick_requests[-1]
    assert request.trip_id == "trip-agency-demo"
    assert request.now == "2026-08-28T10:00:01.500000Z"
