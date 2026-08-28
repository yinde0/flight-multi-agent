from __future__ import annotations

from typing import Protocol

import httpx

from flight_agent.trip_contracts import (
    DocumentStorageStatus,
    SchedulerTickOutcome,
    SchedulerTickRequest,
    StoredTripView,
    TripActivationOutcome,
    validate_sms_notification_input,
)
from flight_agent.telemetry import trace_headers


class TripOrchestratorGateway(Protocol):
    async def activate(
        self,
        *,
        document_bytes: bytes,
        filename: str,
        trip_id: str,
        traveler_ref: str,
        fixture_id: str,
        phone_e164: str | None = None,
        sms_consent: bool = False,
    ) -> TripActivationOutcome: ...

    async def get_trip(self, trip_id: str) -> StoredTripView: ...

    async def document_status(self, trip_id: str) -> DocumentStorageStatus: ...

    async def tick(self, request: SchedulerTickRequest) -> SchedulerTickOutcome: ...


class HttpTripOrchestratorClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 60) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def activate(
        self,
        *,
        document_bytes: bytes,
        filename: str,
        trip_id: str,
        traveler_ref: str,
        fixture_id: str,
        phone_e164: str | None = None,
        sms_consent: bool = False,
    ) -> TripActivationOutcome:
        validated_phone = validate_sms_notification_input(phone_e164, sms_consent)
        data: dict[str, str] = {
            "trip_id": trip_id,
            "traveler_ref": traveler_ref,
            "fixture_id": fixture_id,
            "sms_consent": str(sms_consent).lower(),
        }
        if validated_phone:
            data["phone_e164"] = validated_phone
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            headers=trace_headers(),
        ) as client:
            response = await client.post(
                f"{self._base_url}/v1/trips/activate",
                files={"file": (filename, document_bytes, "application/pdf")},
                data=data,
            )
            response.raise_for_status()
            return TripActivationOutcome.model_validate(response.json())

    async def get_trip(self, trip_id: str) -> StoredTripView:
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            headers=trace_headers(),
        ) as client:
            response = await client.get(f"{self._base_url}/v1/trips/{trip_id}")
            response.raise_for_status()
            return StoredTripView.model_validate(response.json())

    async def document_status(self, trip_id: str) -> DocumentStorageStatus:
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            headers=trace_headers(),
        ) as client:
            response = await client.get(
                f"{self._base_url}/v1/trips/{trip_id}/document-status"
            )
            response.raise_for_status()
            return DocumentStorageStatus.model_validate(response.json())

    async def tick(self, request: SchedulerTickRequest) -> SchedulerTickOutcome:
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            headers=trace_headers(),
        ) as client:
            response = await client.post(
                f"{self._base_url}/v1/scheduler/tick",
                json=request.model_dump(mode="json"),
            )
            response.raise_for_status()
            return SchedulerTickOutcome.model_validate(response.json())
