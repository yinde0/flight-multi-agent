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


MAX_PDF_BYTES = 5 * 1024 * 1024


def create_api_app(
    gateway: DocumentAgentGateway | None = None,
    monitoring_gateway: MonitoringAgentGateway | None = None,
) -> FastAPI:
    app = FastAPI(title="Travel Orchestration API", version="0.2.0")
    app.state.document_gateway = gateway or A2ADocumentAgentClient(
        os.getenv("DOCUMENT_AGENT_URL", "http://127.0.0.1:8001")
    )
    app.state.monitoring_gateway = monitoring_gateway or A2AMonitoringAgentClient(
        os.getenv("MONITOR_AGENT_URL", "http://127.0.0.1:8004")
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

    return app


app = create_api_app()
