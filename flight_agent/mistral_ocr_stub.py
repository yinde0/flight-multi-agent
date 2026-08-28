from __future__ import annotations

import base64
import binascii
import json
import os

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESPONSE_FIXTURE = (
    ROOT
    / "travel_eval"
    / "fixtures"
    / "documents"
    / "mistral_ocr_ambiguous_response.json"
)
EXPECTED_TOKEN = "local-mistral-test-key"
PDF_DATA_PREFIX = "data:application/pdf;base64,"


def _fixture_response() -> dict[str, Any]:
    path = Path(os.getenv("MISTRAL_OCR_STUB_FIXTURE", str(DEFAULT_RESPONSE_FIXTURE)))
    return json.loads(path.read_text(encoding="utf-8"))


def create_mistral_ocr_stub() -> FastAPI:
    """Deterministic local stand-in for Mistral's POST /v1/ocr contract."""

    app = FastAPI(title="Mistral OCR Contract Stub", version="0.1.0")

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/ocr")
    async def process_ocr(
        payload: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if authorization != f"Bearer {EXPECTED_TOKEN}":
            raise HTTPException(status_code=401, detail="Invalid test credential")
        if payload.get("model") != "mistral-ocr-latest":
            raise HTTPException(status_code=400, detail="Unexpected OCR model")

        document = payload.get("document")
        document_url = document.get("document_url") if isinstance(document, dict) else None
        if not isinstance(document_url, str) or not document_url.startswith(
            PDF_DATA_PREFIX
        ):
            raise HTTPException(status_code=400, detail="Expected a base64 PDF data URL")
        try:
            document_bytes = base64.b64decode(
                document_url.removeprefix(PDF_DATA_PREFIX), validate=True
            )
        except (binascii.Error, ValueError) as error:
            raise HTTPException(status_code=400, detail="Invalid base64 PDF") from error
        if not document_bytes.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="Decoded content is not a PDF")

        return _fixture_response()

    return app


app = create_mistral_ocr_stub()
