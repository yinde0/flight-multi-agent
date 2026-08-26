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
from pydantic import BaseModel, Field

from flight_agent.contracts import DocumentMetadata
from flight_agent.parser import extract_pdf_text, parse_extracted_text


class DocumentParsingState(BaseModel):
    document_bytes: bytes = b""
    metadata: dict[str, Any] = Field(default_factory=dict)
    extracted_text: str = ""
    outcome: dict[str, Any] = Field(default_factory=dict)


class DocumentParsingFlow(Flow[DocumentParsingState]):
    """CrewAI Flow for deterministic, auditable document parsing."""

    tracing: bool | None = False
    suppress_flow_events: bool = True

    # This slice has no RAG or conversational memory. Avoid allocating CrewAI's
    # vector-memory backend for a two-step, stateless parsing flow.
    _skip_auto_memory: ClassVar[bool] = True

    @start()
    def extract_text_layer(self) -> str:
        self.state.extracted_text = extract_pdf_text(self.state.document_bytes)
        return self.state.extracted_text

    @listen(extract_text_layer)
    def build_canonical_result(self, extracted_text: str) -> dict[str, Any]:
        metadata = DocumentMetadata.model_validate(self.state.metadata)
        result = parse_extracted_text(extracted_text, metadata)
        self.state.outcome = result.model_dump(mode="json", exclude_none=True)
        return self.state.outcome


def run_document_flow(
    document_bytes: bytes, metadata: DocumentMetadata
) -> dict[str, Any]:
    flow = DocumentParsingFlow()
    result = flow.kickoff(
        inputs={
            "document_bytes": document_bytes,
            "metadata": metadata.model_dump(mode="json"),
        }
    )
    if not isinstance(result, dict):
        raise TypeError("DocumentParsingFlow must return a JSON object")
    return result
