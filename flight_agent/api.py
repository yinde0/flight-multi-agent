from __future__ import annotations

import hashlib
import os

from datetime import timedelta

import httpx

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from flight_agent.a2a_client import A2ADocumentAgentClient, DocumentAgentGateway
from flight_agent.contracts import DocumentMetadata, ParseOutcome
from flight_agent.flight_agency_client import (
    FlightAgencyGateway,
    HttpFlightAgencyClient,
)
from flight_agent.flight_agency_contracts import (
    AgencyDemoStatus,
    AgencyFlightCollection,
    AgencyFlightDetails,
    AgencyFlightMutation,
    AgencyTripSyncOutcome,
)
from flight_agent.monitoring_a2a_client import (
    A2AMonitoringAgentClient,
    MonitoringAgentGateway,
)
from flight_agent.monitoring_contracts import (
    MonitoringPollOutcome,
    MonitoringPollRequest,
)
from flight_agent.trip_contracts import (
    DocumentStorageStatus,
    SchedulerTickOutcome,
    SchedulerTickRequest,
    StoredTripView,
    TripActivationOutcome,
    validate_sms_notification_input,
)
from flight_agent.trip_orchestrator_client import (
    HttpTripOrchestratorClient,
    TripOrchestratorGateway,
)
from flight_agent.telemetry import (
    hash_reference,
    install_telemetry_routes,
    set_current_span_attributes,
    set_current_span_content,
)
from travel_eval.clock import parse_timestamp


MAX_PDF_BYTES = 5 * 1024 * 1024


def create_api_app(
    gateway: DocumentAgentGateway | None = None,
    monitoring_gateway: MonitoringAgentGateway | None = None,
    trip_gateway: TripOrchestratorGateway | None = None,
    scheduler_control_enabled: bool | None = None,
    flight_agency_gateway: FlightAgencyGateway | None = None,
    flight_agency_demo_enabled: bool | None = None,
) -> FastAPI:
    app = FastAPI(title="Travel Orchestration API", version="0.2.0")
    install_telemetry_routes(app, service_name="travel-api")
    app.state.document_gateway = gateway or A2ADocumentAgentClient(
        os.getenv("DOCUMENT_AGENT_URL", "http://127.0.0.1:8001")
    )
    app.state.monitoring_gateway = monitoring_gateway or A2AMonitoringAgentClient(
        os.getenv("MONITOR_AGENT_URL", "http://127.0.0.1:8004")
    )
    app.state.trip_gateway = trip_gateway or HttpTripOrchestratorClient(
        os.getenv("TRIP_ORCHESTRATOR_URL", "http://127.0.0.1:8011")
    )
    app.state.scheduler_control_enabled = (
        scheduler_control_enabled
        if scheduler_control_enabled is not None
        else os.getenv("SCHEDULER_CONTROL_ENABLED", "false").lower() == "true"
    )
    app.state.flight_agency_demo_enabled = (
        flight_agency_demo_enabled
        if flight_agency_demo_enabled is not None
        else os.getenv("FLIGHT_AGENCY_DEMO_ENABLED", "false").lower()
        in {"1", "true", "yes"}
    )
    app.state.flight_agency_gateway = flight_agency_gateway
    if app.state.flight_agency_demo_enabled and app.state.flight_agency_gateway is None:
        app.state.flight_agency_gateway = HttpFlightAgencyClient(
            os.getenv(
                "FLIGHT_AGENCY_BASE_URL", "http://flight-agency-simulator:8015"
            ),
            control_token=os.getenv("FLIGHT_AGENCY_CONTROL_TOKEN", ""),
            timeout_seconds=float(os.getenv("FLIGHT_AGENCY_TIMEOUT_SECONDS", "15")),
        )

    def agency_gateway() -> FlightAgencyGateway:
        if not app.state.flight_agency_demo_enabled:
            raise HTTPException(status_code=404, detail="Flight agency demo is disabled")
        resolved: FlightAgencyGateway | None = app.state.flight_agency_gateway
        if resolved is None:
            raise HTTPException(status_code=503, detail="Flight agency demo is unavailable")
        return resolved

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/demo/agency/status", response_model=AgencyDemoStatus)
    async def agency_demo_status() -> AgencyDemoStatus:
        agency = agency_gateway()
        try:
            flight_count = await agency.health()
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502, detail="Flight agency sandbox is unavailable"
            ) from error
        return AgencyDemoStatus(enabled=True, flight_count=flight_count)

    @app.get("/v1/demo/agency/flights", response_model=AgencyFlightCollection)
    async def agency_flights() -> AgencyFlightCollection:
        try:
            return await agency_gateway().list_flights()
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502, detail="Flight agency sandbox is unavailable"
            ) from error

    @app.post(
        "/v1/demo/agency/trips/{trip_id}/sync",
        response_model=AgencyTripSyncOutcome,
    )
    async def sync_agency_trip(trip_id: str) -> AgencyTripSyncOutcome:
        trip_orchestrator: TripOrchestratorGateway = app.state.trip_gateway
        try:
            trip = await trip_orchestrator.get_trip(trip_id)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                raise HTTPException(status_code=404, detail="Trip not found") from error
            raise HTTPException(
                status_code=502, detail="Trip Orchestrator is unavailable"
            ) from error
        if trip.itinerary is None:
            raise HTTPException(
                status_code=409, detail="Trip itinerary requires review before simulation"
            )
        try:
            collection = await agency_gateway().seed_itinerary(trip.itinerary)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 409:
                raise HTTPException(
                    status_code=409,
                    detail="Flight agency already has a conflicting flight schedule",
                ) from error
            raise HTTPException(
                status_code=502, detail="Flight agency sandbox is unavailable"
            ) from error
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502, detail="Flight agency sandbox is unavailable"
            ) from error
        return AgencyTripSyncOutcome(trip_id=trip_id, flights=collection.flights)

    @app.patch(
        "/v1/demo/agency/flights/{flight_iata}/{flight_date}",
        response_model=AgencyFlightDetails,
    )
    async def change_agency_flight(
        flight_iata: str,
        flight_date: str,
        request: AgencyFlightMutation,
    ) -> AgencyFlightDetails:
        try:
            return await agency_gateway().change_flight(
                flight_iata.upper(), flight_date, request
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                raise HTTPException(status_code=404, detail="Flight not found") from error
            raise HTTPException(
                status_code=502, detail="Flight agency sandbox is unavailable"
            ) from error
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502, detail="Flight agency sandbox is unavailable"
            ) from error

    @app.post(
        "/v1/demo/agency/flights/{flight_iata}/{flight_date}/reset",
        response_model=AgencyFlightDetails,
    )
    async def reset_agency_flight(
        flight_iata: str, flight_date: str
    ) -> AgencyFlightDetails:
        try:
            return await agency_gateway().reset_flight(
                flight_iata.upper(), flight_date
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                raise HTTPException(status_code=404, detail="Flight not found") from error
            raise HTTPException(
                status_code=502, detail="Flight agency sandbox is unavailable"
            ) from error
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502, detail="Flight agency sandbox is unavailable"
            ) from error

    @app.post(
        "/v1/demo/agency/trips/{trip_id}/check",
        response_model=SchedulerTickOutcome,
    )
    async def run_agency_demo_check(trip_id: str) -> SchedulerTickOutcome:
        if not app.state.scheduler_control_enabled:
            raise HTTPException(status_code=404, detail="Scheduler control is disabled")
        agency_gateway()
        trip_orchestrator: TripOrchestratorGateway = app.state.trip_gateway
        try:
            trip = await trip_orchestrator.get_trip(trip_id)
            next_checks = [
                parse_timestamp(leg.next_poll_at)
                for leg in trip.legs
                if leg.monitoring_status == "active" and leg.next_poll_at
            ]
            if not next_checks:
                raise HTTPException(
                    status_code=409, detail="Trip has no active flights to check"
                )
            check_at = min(next_checks) + timedelta(seconds=1)
            return await trip_orchestrator.tick(
                SchedulerTickRequest(
                    now=check_at.isoformat().replace("+00:00", "Z"),
                    maximum_legs=max(1, len(trip.legs)),
                    trip_id=trip_id,
                )
            )
        except HTTPException:
            raise
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                raise HTTPException(status_code=404, detail="Trip not found") from error
            raise HTTPException(
                status_code=502, detail="Trip Orchestrator is unavailable"
            ) from error
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502, detail="Trip Orchestrator is unavailable"
            ) from error

    @app.post(
        "/v1/documents/parse",
        response_model=ParseOutcome,
        response_model_exclude_none=True,
    )
    async def parse_document(
        file: UploadFile = File(...),
        trip_id: str = Form(...),
        traveler_ref: str = Form(...),
        fixture_id: str = Form(...),
    ) -> ParseOutcome:
        document_bytes = await file.read(MAX_PDF_BYTES + 1)
        if len(document_bytes) > MAX_PDF_BYTES:
            raise HTTPException(status_code=413, detail="PDF exceeds 5 MiB limit")
        if file.content_type != "application/pdf" or not document_bytes.startswith(
            b"%PDF-"
        ):
            raise HTTPException(status_code=415, detail="A PDF upload is required")

        metadata = DocumentMetadata(
            trip_id=trip_id,
            traveler_ref=traveler_ref,
            fixture_id=fixture_id,
            filename=file.filename or "itinerary.pdf",
            sha256=hashlib.sha256(document_bytes).hexdigest(),
        )
        gateway: DocumentAgentGateway = app.state.document_gateway
        try:
            return await gateway.parse(document_bytes, metadata)
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502, detail="Document agent is unavailable"
            ) from error
        except RuntimeError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.post(
        "/v1/monitoring/poll",
        response_model=MonitoringPollOutcome,
        response_model_exclude_none=True,
    )
    async def poll_flight(
        request: MonitoringPollRequest,
    ) -> MonitoringPollOutcome:
        monitoring: MonitoringAgentGateway = app.state.monitoring_gateway
        try:
            return await monitoring.poll(request)
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502, detail="Monitoring Agent is unavailable"
            ) from error
        except RuntimeError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.post(
        "/v1/trips/activate",
        response_model=TripActivationOutcome,
        response_model_exclude_none=True,
    )
    async def activate_trip(
        file: UploadFile = File(...),
        trip_id: str = Form(...),
        traveler_ref: str = Form(...),
        fixture_id: str = Form(...),
        phone_e164: str | None = Form(default=None),
        sms_consent: bool = Form(default=False),
    ) -> TripActivationOutcome:
        document_bytes = await file.read(MAX_PDF_BYTES + 1)
        if len(document_bytes) > MAX_PDF_BYTES:
            raise HTTPException(status_code=413, detail="PDF exceeds 5 MiB limit")
        if file.content_type != "application/pdf" or not document_bytes.startswith(
            b"%PDF-"
        ):
            raise HTTPException(status_code=415, detail="A PDF upload is required")
        try:
            validated_phone = validate_sms_notification_input(
                phone_e164, sms_consent
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        set_current_span_attributes(
            {
                "travel.trip_ref": hash_reference(trip_id),
                "travel.fixture_ref": hash_reference(fixture_id),
            }
        )
        set_current_span_content(
            input_value={
                "task": "Activate a booked trip and hand it to continuous monitoring.",
                "document": {
                    "media_type": "application/pdf",
                    "byte_count": len(document_bytes),
                    "document_ref": hash_reference(
                        hashlib.sha256(document_bytes).hexdigest()
                    ),
                },
                "correlation": {
                    "trip_ref": hash_reference(trip_id),
                    "traveler_ref": hash_reference(traveler_ref),
                    "fixture_ref": hash_reference(fixture_id),
                },
                "notification": {
                    "sms_consent": sms_consent,
                    "phone_supplied": validated_phone is not None,
                },
            }
        )
        trip_orchestrator: TripOrchestratorGateway = app.state.trip_gateway
        try:
            outcome = await trip_orchestrator.activate(
                document_bytes=document_bytes,
                filename=file.filename or "itinerary.pdf",
                trip_id=trip_id,
                traveler_ref=traveler_ref,
                fixture_id=fixture_id,
                phone_e164=validated_phone,
                sms_consent=sms_consent,
            )
            set_current_span_content(
                output_value={
                    "status": outcome.status,
                    "trip_status": outcome.trip_status,
                    "parse_status": outcome.parse_status,
                    "active_leg_count": outcome.active_leg_count,
                    "next_poll_at": outcome.next_poll_at,
                    "idempotent_replay": outcome.idempotent_replay,
                }
            )
            return outcome
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 409:
                raise HTTPException(
                    status_code=409, detail="Trip ID already has different evidence"
                ) from error
            raise HTTPException(
                status_code=502, detail="Trip Orchestrator is unavailable"
            ) from error
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502, detail="Trip Orchestrator is unavailable"
            ) from error

    @app.get("/v1/trips/{trip_id}", response_model=StoredTripView)
    async def get_trip(trip_id: str) -> StoredTripView:
        trip_orchestrator: TripOrchestratorGateway = app.state.trip_gateway
        try:
            return await trip_orchestrator.get_trip(trip_id)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                raise HTTPException(status_code=404, detail="Trip not found") from error
            raise HTTPException(
                status_code=502, detail="Trip Orchestrator is unavailable"
            ) from error
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502, detail="Trip Orchestrator is unavailable"
            ) from error

    @app.post("/v1/orchestration/tick", response_model=SchedulerTickOutcome)
    async def scheduler_tick(request: SchedulerTickRequest) -> SchedulerTickOutcome:
        if not app.state.scheduler_control_enabled:
            raise HTTPException(status_code=404, detail="Scheduler control is disabled")
        trip_orchestrator: TripOrchestratorGateway = app.state.trip_gateway
        try:
            return await trip_orchestrator.tick(request)
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502, detail="Trip Orchestrator is unavailable"
            ) from error

    @app.get(
        "/v1/trips/{trip_id}/document-status",
        response_model=DocumentStorageStatus,
    )
    async def document_status(trip_id: str) -> DocumentStorageStatus:
        trip_orchestrator: TripOrchestratorGateway = app.state.trip_gateway
        try:
            return await trip_orchestrator.document_status(trip_id)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                raise HTTPException(status_code=404, detail="Trip not found") from error
            raise HTTPException(
                status_code=502, detail="Trip Orchestrator is unavailable"
            ) from error
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502, detail="Trip Orchestrator is unavailable"
            ) from error

    return app


app = create_api_app()
