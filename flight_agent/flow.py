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

from flight_agent.contracts import DocumentMetadata
from flight_agent.ocr import (
    MistralOcrProvider,
    OcrError,
    OcrNotConfiguredError,
    OcrProvider,
)
from flight_agent.parser import extract_pdf_text, parse_extracted_text, review_outcome


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

    def __init__(self, *, ocr_provider: OcrProvider | None = None) -> None:
        super().__init__()
        self._ocr_provider = ocr_provider or MistralOcrProvider.from_environment()

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
        self.state.outcome = result.model_dump(mode="json", exclude_none=True)
        return self.state.outcome


def run_document_flow(
    document_bytes: bytes,
    metadata: DocumentMetadata,
    ocr_provider: OcrProvider | None = None,
) -> dict[str, Any]:
    flow = DocumentParsingFlow(ocr_provider=ocr_provider)
    result = flow.kickoff(
        inputs={
            "document_bytes": document_bytes,
            "metadata": metadata.model_dump(mode="json"),
        }
    )
    if not isinstance(result, dict):
        raise TypeError("DocumentParsingFlow must return a JSON object")
    return result
