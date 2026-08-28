from __future__ import annotations

from typing import Protocol

import httpx

from flight_agent.contracts import CanonicalItinerary
from flight_agent.flight_agency_contracts import (
    AgencyFlightCollection,
    AgencyFlightDetails,
    AgencyFlightMutation,
    AgencyFlightSeed,
    AgencyFlightSeedBatch,
)
from travel_eval.clock import parse_timestamp


class FlightAgencyGateway(Protocol):
    async def health(self) -> int: ...

    async def seed_itinerary(
        self, itinerary: CanonicalItinerary
    ) -> AgencyFlightCollection: ...

    async def list_flights(self) -> AgencyFlightCollection: ...

    async def change_flight(
        self,
        flight_iata: str,
        flight_date: str,
        mutation: AgencyFlightMutation,
    ) -> AgencyFlightDetails: ...

    async def reset_flight(
        self, flight_iata: str, flight_date: str
    ) -> AgencyFlightDetails: ...


class HttpFlightAgencyClient:
    def __init__(
        self,
        base_url: str,
        *,
        control_token: str,
        timeout_seconds: float = 15,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Flight-Agency-Token": control_token}
        self._timeout_seconds = timeout_seconds

    async def health(self) -> int:
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds, trust_env=False
        ) as client:
            response = await client.get(f"{self._base_url}/health/live")
            response.raise_for_status()
            payload = response.json()
            return max(0, int(payload.get("flight_count", 0)))

    async def seed_itinerary(
        self, itinerary: CanonicalItinerary
    ) -> AgencyFlightCollection:
        flights = []
        for index, leg in enumerate(itinerary.legs, start=1):
            flights.append(
                AgencyFlightSeed(
                    flight_iata=leg.flight_number,
                    flight_date=parse_timestamp(
                        leg.scheduled_departure_at
                    ).date().isoformat(),
                    origin=leg.origin,
                    destination=leg.destination,
                    scheduled_departure_at=leg.scheduled_departure_at,
                    scheduled_arrival_at=leg.scheduled_arrival_at,
                    departure_terminal="1",
                    departure_gate=f"A{index + 9}",
                    arrival_terminal="1",
                    arrival_gate=f"B{index + 9}",
                )
            )
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            headers=self._headers,
            trust_env=False,
        ) as client:
            response = await client.post(
                f"{self._base_url}/v1/flights",
                json=AgencyFlightSeedBatch(
                    flights=flights, reset_existing=True
                ).model_dump(mode="json"),
            )
            response.raise_for_status()
            return AgencyFlightCollection.model_validate(response.json())

    async def list_flights(self) -> AgencyFlightCollection:
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            headers=self._headers,
            trust_env=False,
        ) as client:
            response = await client.get(f"{self._base_url}/v1/flights")
            response.raise_for_status()
            return AgencyFlightCollection.model_validate(response.json())

    async def change_flight(
        self,
        flight_iata: str,
        flight_date: str,
        mutation: AgencyFlightMutation,
    ) -> AgencyFlightDetails:
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            headers=self._headers,
            trust_env=False,
        ) as client:
            response = await client.patch(
                f"{self._base_url}/v1/flights/{flight_iata}/{flight_date}",
                json=mutation.model_dump(mode="json", exclude_none=True),
            )
            response.raise_for_status()
            return AgencyFlightDetails.model_validate(response.json())

    async def reset_flight(
        self, flight_iata: str, flight_date: str
    ) -> AgencyFlightDetails:
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            headers=self._headers,
            trust_env=False,
        ) as client:
            response = await client.post(
                f"{self._base_url}/v1/flights/{flight_iata}/{flight_date}/reset"
            )
            response.raise_for_status()
            return AgencyFlightDetails.model_validate(response.json())
