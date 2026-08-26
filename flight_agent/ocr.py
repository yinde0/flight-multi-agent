from __future__ import annotations

import base64
import os

from dataclasses import dataclass
from typing import Any, Protocol

import httpx


DEFAULT_MISTRAL_OCR_BASE_URL = "https://api.mistral.ai/v1"
DEFAULT_MISTRAL_OCR_MODEL = "mistral-ocr-latest"


class OcrError(RuntimeError):
    """The configured OCR provider could not return usable document text."""


class OcrNotConfiguredError(OcrError):
    """OCR is needed for this document, but no provider credential is configured."""


@dataclass(frozen=True)
class OcrExtraction:
    text: str
    provider: str
    model: str
    page_count: int


class OcrProvider(Protocol):
    def extract_pdf(self, document_bytes: bytes) -> OcrExtraction:
        """Extract text from a PDF without inventing missing document content."""


class MistralOcrProvider:
    """Small adapter around Mistral's POST /v1/ocr REST contract."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_MISTRAL_OCR_BASE_URL,
        model: str = DEFAULT_MISTRAL_OCR_MODEL,
        timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    @classmethod
    def from_environment(cls) -> "MistralOcrProvider":
        return cls(
            api_key=os.getenv("MISTRAL_API_KEY", ""),
            base_url=os.getenv(
                "MISTRAL_OCR_BASE_URL", DEFAULT_MISTRAL_OCR_BASE_URL
            ),
            model=os.getenv("MISTRAL_OCR_MODEL", DEFAULT_MISTRAL_OCR_MODEL),
            timeout_seconds=float(os.getenv("MISTRAL_OCR_TIMEOUT_SECONDS", "60")),
        )

    def extract_pdf(self, document_bytes: bytes) -> OcrExtraction:
        if not self._api_key:
            raise OcrNotConfiguredError(
                "MISTRAL_API_KEY is required for image-only PDFs"
            )

        encoded_pdf = base64.b64encode(document_bytes).decode("ascii")
        payload = {
            "model": self._model,
            "document": {
                "type": "document_url",
                "document_url": f"data:application/pdf;base64,{encoded_pdf}",
            },
            "include_image_base64": False,
            "confidence_scores_granularity": "page",
        }

        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.post(
                    f"{self._base_url}/ocr",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                body: Any = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise OcrError("Mistral OCR request failed") from error

        if not isinstance(body, dict) or not isinstance(body.get("pages"), list):
            raise OcrError("Mistral OCR returned an invalid response")

        page_markdown = [
            page.get("markdown", "").strip()
            for page in body["pages"]
            if isinstance(page, dict)
        ]
        text = "\n\n".join(page for page in page_markdown if page).strip()
        if not text:
            raise OcrError("Mistral OCR returned no text")

        response_model = body.get("model")
        return OcrExtraction(
            text=text,
            provider="mistral",
            model=response_model if isinstance(response_model, str) else self._model,
            page_count=len(body["pages"]),
        )
