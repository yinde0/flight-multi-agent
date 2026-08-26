from __future__ import annotations

import hashlib
import os

import httpx

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from flight_agent.a2a_client import A2ADocumentAgentClient, DocumentAgentGateway
from flight_agent.contracts import DocumentMetadata, ParseOutcome


MAX_PDF_BYTES = 5 * 1024 * 1024


def create_api_app(gateway: DocumentAgentGateway | None = None) -> FastAPI:
    app = FastAPI(title="Travel Itinerary API", version="0.1.0")
    app.state.document_gateway = gateway or A2ADocumentAgentClient(
        os.getenv("DOCUMENT_AGENT_URL", "http://127.0.0.1:8001")
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

    return app


app = create_api_app()
