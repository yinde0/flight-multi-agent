from __future__ import annotations

import os
import tempfile

from pathlib import Path
from typing import Any, ClassVar

# CrewAI resolves its storage directory during import. Keep runtime files ephemeral
# and disable outbound telemetry for this deterministic, zero-LLM flow.
if os.name == "nt":
    # CrewAI's credential manager uses LOCALAPPDATA independently of its flow storage.
    os.environ["LOCALAPPDATA"] = str(
        Path(tempfile.gettempdir()) / "flight-agent-runtime"
    )
os.environ.setdefault(
    "CREWAI_STORAGE_DIR", str(Path(tempfile.gettempdir()) / "flight-agent-crewai")
)
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_DISABLE_TRACKING", "true")
os.environ.setdefault("CREWAI_DISABLE_VERSION_CHECK", "true")

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel, Field, PrivateAttr

from flight_agent.contracts import DocumentMetadata, ParseOutcome
from flight_agent.itinerary_llm import (
    AzureOpenAIItineraryProvider,
    DEFAULT_MIN_CONFIDENCE,
    ItineraryLlmError,
    ItineraryLlmNotConfiguredError,
    ItineraryLlmProvider,
    resolve_review_with_llm,
)
from flight_agent.ocr import (
    MistralOcrProvider,
    OcrError,
    OcrNotConfiguredError,
    OcrProvider,
)
from flight_agent.parser import extract_pdf_text, parse_extracted_text, review_outcome
from flight_agent.provider_mcp_clients import (
    StreamableHttpItineraryLlmMcpClient,
    StreamableHttpOcrMcpClient,
)
from flight_agent.telemetry import hash_reference, traced


class DocumentParsingState(BaseModel):
    document_bytes: bytes = b""
    metadata: dict[str, Any] = Field(default_factory=dict)
    extracted_text: str = ""
    text_source: str = "pdf_text_layer"
    ocr_details: dict[str, Any] = Field(default_factory=dict)
    extraction_error: str | None = None
    outcome: dict[str, Any] = Field(default_factory=dict)


class DocumentParsingFlow(Flow[DocumentParsingState]):
    """CrewAI Flow for deterministic, auditable document parsing."""

    tracing: bool | None = False
    suppress_flow_events: bool = True

    # This slice has no RAG or conversational memory. Avoid allocating CrewAI's
    # vector-memory backend for a two-step, stateless parsing flow.
    _skip_auto_memory: ClassVar[bool] = True
    _ocr_provider: OcrProvider = PrivateAttr()
    _llm_provider: ItineraryLlmProvider = PrivateAttr()
    _llm_mode: str = PrivateAttr()
    _llm_min_confidence: float = PrivateAttr()

    def __init__(
        self,
        *,
        ocr_provider: OcrProvider | None = None,
        llm_provider: ItineraryLlmProvider | None = None,
        llm_mode: str | None = None,
        llm_min_confidence: float | None = None,
    ) -> None:
        super().__init__()
        external_mode = os.getenv("EXTERNAL_CALLS_PROVIDER", "direct").strip().lower()
        if external_mode not in {"direct", "mcp"}:
            raise ValueError("EXTERNAL_CALLS_PROVIDER must be direct or mcp")
        self._ocr_provider = ocr_provider or (
            StreamableHttpOcrMcpClient()
            if external_mode == "mcp"
            else MistralOcrProvider.from_environment()
        )
        self._llm_provider = llm_provider or (
            StreamableHttpItineraryLlmMcpClient()
            if external_mode == "mcp"
            else AzureOpenAIItineraryProvider.from_environment()
        )
        self._llm_mode = (
            llm_mode or os.getenv("DOCUMENT_LLM_MODE", "off")
        ).strip().lower()
        if self._llm_mode not in {"off", "fallback"}:
            raise ValueError("DOCUMENT_LLM_MODE must be off or fallback")
        self._llm_min_confidence = (
            llm_min_confidence
            if llm_min_confidence is not None
            else float(
                os.getenv(
                    "DOCUMENT_LLM_MIN_CONFIDENCE",
                    str(DEFAULT_MIN_CONFIDENCE),
                )
            )
        )

    @start()
    def extract_text_layer(self) -> str:
        self.state.extracted_text = extract_pdf_text(self.state.document_bytes)
        if self.state.extracted_text:
            self.state.text_source = "pdf_text_layer"
            return self.state.extracted_text

        try:
            extraction = self._ocr_provider.extract_pdf(self.state.document_bytes)
        except OcrNotConfiguredError:
            self.state.extraction_error = "OCR_NOT_CONFIGURED"
            return ""
        except OcrError:
            self.state.extraction_error = "OCR_PROCESSING_FAILED"
            return ""

        self.state.extracted_text = extraction.text
        self.state.text_source = "mistral_ocr"
        self.state.ocr_details = {
            "provider": extraction.provider,
            "model": extraction.model,
            "page_count": extraction.page_count,
        }
        return self.state.extracted_text

    @listen(extract_text_layer)
    def build_canonical_result(self, extracted_text: str) -> dict[str, Any]:
        metadata = DocumentMetadata.model_validate(self.state.metadata)
        if self.state.extraction_error:
            attempted_ocr = self.state.extraction_error == "OCR_PROCESSING_FAILED"
            steps = ["extract_pdf_text"]
            if attempted_ocr:
                steps.append("mistral_ocr")
            steps.append("request_human_review")
            result = review_outcome(
                metadata,
                reason_codes=[self.state.extraction_error],
                orchestration={
                    "framework": "crewai-flow",
                    "steps": steps,
                    "llm_calls": 0,
                    "ocr_calls": 1 if attempted_ocr else 0,
                    "text_source": "none",
                },
            )
        else:
            result = parse_extracted_text(
                extracted_text,
                metadata,
                text_source=self.state.text_source,
                ocr_details=self.state.ocr_details or None,
            )
            if (
                result.status == "review_required"
                and extracted_text.strip()
                and self._llm_mode == "fallback"
            ):
                try:
                    result = resolve_review_with_llm(
                        extracted_text,
                        metadata,
                        self._llm_provider,
                        result,
                        self._llm_min_confidence,
                    )
                except ItineraryLlmNotConfiguredError:
                    result = self._llm_failure_review(
                        result,
                        metadata,
                        reason_code="LLM_NOT_CONFIGURED",
                        attempted=False,
                    )
                except ItineraryLlmError:
                    result = self._llm_failure_review(
                        result,
                        metadata,
                        reason_code="LLM_EXTRACTION_FAILED",
                        attempted=True,
                    )
        self.state.outcome = result.model_dump(mode="json", exclude_none=True)
        return self.state.outcome

    def _llm_failure_review(
        self,
        base: ParseOutcome,
        metadata: DocumentMetadata,
        *,
        reason_code: str,
        attempted: bool,
    ) -> ParseOutcome:
        orchestration = dict(base.orchestration)
        steps = list(orchestration.get("steps", []))
        if attempted:
            steps.insert(
                max(0, len(steps) - 1), "azure_openai_extract_itinerary"
            )
        orchestration.update(
            {
                "steps": steps,
                "llm_calls": 1 if attempted else 0,
                "llm": {
                    "provider": self._llm_provider.provider_name,
                    "model": self._llm_provider.model_name,
                    "result": "failed" if attempted else "not_configured",
                },
            }
        )
        review = base.review or {}
        return review_outcome(
            metadata,
            reason_codes=[reason_code, *review.get("reason_codes", [])],
            safe_partial_extraction=review.get("safe_partial_extraction", {}),
            must_not_infer=review.get("must_not_infer"),
            orchestration=orchestration,
        )


def document_agent_trace_input(
    document_bytes: bytes,
    metadata: DocumentMetadata,
    ocr_provider: OcrProvider | None = None,
    llm_provider: ItineraryLlmProvider | None = None,
    llm_mode: str | None = None,
    llm_min_confidence: float | None = None,
) -> dict[str, Any]:
    """Development trace input without ticket bytes or traveler identifiers."""

    return {
        "task": "Extract booked flight legs into the canonical itinerary schema.",
        "document": {
            "media_type": metadata.media_type,
            "byte_count": len(document_bytes),
            "document_ref": hash_reference(metadata.sha256),
        },
        "correlation": {
            "trip_ref": hash_reference(metadata.trip_id),
            "traveler_ref": hash_reference(metadata.traveler_ref),
            "fixture_ref": hash_reference(metadata.fixture_id),
        },
        "ocr_provider_supplied": ocr_provider is not None,
        "llm_fallback_mode": llm_mode or os.getenv("DOCUMENT_LLM_MODE", "off"),
        "llm_provider_supplied": llm_provider is not None,
    }


def document_agent_trace_output(result: dict[str, Any]) -> dict[str, Any]:
    """Expose useful extraction results while omitting booking secrets and PII."""

    itinerary = result.get("itinerary") or {}
    legs = itinerary.get("legs") or []
    output: dict[str, Any] = {
        "status": result.get("status"),
        "itinerary": {
            "confirmation_count": len(itinerary.get("confirmation_codes") or []),
            "leg_count": len(legs),
            "legs": [
                {
                    "leg_ref": hash_reference(leg.get("leg_id", "")),
                    "flight_number": leg.get("flight_number"),
                    "origin": leg.get("origin"),
                    "destination": leg.get("destination"),
                    "scheduled_departure_at": leg.get("scheduled_departure_at"),
                    "scheduled_arrival_at": leg.get("scheduled_arrival_at"),
                }
                for leg in legs
            ],
        },
        "orchestration": result.get("orchestration", {}),
    }
    review = result.get("review")
    if isinstance(review, dict):
        output["review"] = {
            "reason_codes": review.get("reason_codes", []),
            "missing_fields": review.get("missing_fields", []),
        }
    return output


@traced(
    "agent.document.parse_itinerary",
    service_name="document-agent",
    attributes=lambda document_bytes, metadata, *_args, **_kwargs: {
        "travel.trip_ref": hash_reference(metadata.trip_id),
        "travel.document_bytes": len(document_bytes),
    },
    result_outcome=lambda result: str(result.get("status", "completed")),
    content_input=document_agent_trace_input,
    content_output=document_agent_trace_output,
)
def run_document_flow(
    document_bytes: bytes,
    metadata: DocumentMetadata,
    ocr_provider: OcrProvider | None = None,
    llm_provider: ItineraryLlmProvider | None = None,
    llm_mode: str | None = None,
    llm_min_confidence: float | None = None,
) -> dict[str, Any]:
    flow = DocumentParsingFlow(
        ocr_provider=ocr_provider,
        llm_provider=llm_provider,
        llm_mode=llm_mode,
        llm_min_confidence=llm_min_confidence,
    )
    result = flow.kickoff(
        inputs={
            "document_bytes": document_bytes,
            "metadata": metadata.model_dump(mode="json"),
        }
    )
    if not isinstance(result, dict):
        raise TypeError("DocumentParsingFlow must return a JSON object")
    return result
