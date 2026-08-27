from __future__ import annotations

import hashlib
import os

import httpx

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from flight_agent.a2a_client import A2ADocumentAgentClient, DocumentAgentGateway
from flight_agent.contracts import DocumentMetadata, ParseOutcome
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
)
from flight_agent.trip_orchestrator_client import (
    HttpTripOrchestratorClient,
    TripOrchestratorGateway,
)


MAX_PDF_BYTES = 5 * 1024 * 1024


def create_api_app(
    gateway: DocumentAgentGateway | None = None,
    monitoring_gateway: MonitoringAgentGateway | None = None,
    trip_gateway: TripOrchestratorGateway | None = None,
    scheduler_control_enabled: bool | None = None,
) -> FastAPI:
    app = FastAPI(title="Travel Orchestration API", version="0.2.0")
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

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

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
    ) -> TripActivationOutcome:
        document_bytes = await file.read(MAX_PDF_BYTES + 1)
        if len(document_bytes) > MAX_PDF_BYTES:
            raise HTTPException(status_code=413, detail="PDF exceeds 5 MiB limit")
        if file.content_type != "application/pdf" or not document_bytes.startswith(
            b"%PDF-"
        ):
            raise HTTPException(status_code=415, detail="A PDF upload is required")
        trip_orchestrator: TripOrchestratorGateway = app.state.trip_gateway
        try:
            return await trip_orchestrator.activate(
                document_bytes=document_bytes,
                filename=file.filename or "itinerary.pdf",
                trip_id=trip_id,
                traveler_ref=traveler_ref,
                fixture_id=fixture_id,
            )
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
