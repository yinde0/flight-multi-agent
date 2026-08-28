from __future__ import annotations

import hashlib
import json

from pathlib import Path

import httpx
import pytest

from flight_agent.contracts import DocumentMetadata, ParseOutcome
from flight_agent.flow import run_document_flow
from flight_agent.itinerary_llm import (
    AzureOpenAIItineraryProvider,
    ItineraryLlmError,
    ItineraryLlmNotConfiguredError,
    LlmItineraryExtraction,
    llm_trace_input,
)
from flight_agent.ocr import OcrExtraction, OcrNotConfiguredError
from flight_agent.parser import parse_extracted_text


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "travel_eval" / "fixtures" / "documents"
PDF_ROOT = ROOT / "output" / "pdf"
ALTERNATE_TEXT = (FIXTURES / "alternate_layout_ticket.txt").read_text(
    encoding="utf-8"
)
EXTRACTION = LlmItineraryExtraction.model_validate_json(
    (FIXTURES / "azure_openai_itinerary_extraction.json").read_text(
        encoding="utf-8"
    )
)


def metadata(document: bytes, *, suffix: str = "llm") -> DocumentMetadata:
    return DocumentMetadata(
        trip_id=f"trip-fixture-{suffix}",
        traveler_ref="traveler-synthetic-001",
        fixture_id=f"doc-fixture-{suffix}",
        filename="synthetic-alternate-layout.pdf",
        sha256=hashlib.sha256(document).hexdigest(),
    )


class AlternateTextOcr:
    def extract_pdf(self, document_bytes: bytes) -> OcrExtraction:
        return OcrExtraction(
            text=ALTERNATE_TEXT,
            provider="fixture",
            model="fixture-ocr",
            page_count=1,
        )


class NeverOcr:
    def extract_pdf(self, document_bytes: bytes) -> OcrExtraction:
        raise OcrNotConfiguredError("OCR should not be called")


class FixtureItineraryLlm:
    provider_name = "azure_openai"
    model_name = "fixture-gpt-deployment"

    def __init__(self, extraction: LlmItineraryExtraction = EXTRACTION) -> None:
        self.extraction = extraction
        self.calls: list[str] = []

    def extract_itinerary(self, text: str) -> LlmItineraryExtraction:
        self.calls.append(text)
        return self.extraction


def test_azure_adapter_accepts_existing_chat_environment_aliases(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_ENDPOINT", "https://azure.test")
    monkeypatch.setenv("AZURE_API_KEY", "test-key")
    monkeypatch.setenv("CHAT_DEPLOYMENT", "chat-gpt-4-1-mini")
    monkeypatch.setenv("CHAT_API_VERSION", "2025-01-01-preview")
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)

    provider = AzureOpenAIItineraryProvider.from_environment()

    assert provider.model_name == "chat-gpt-4-1-mini"
    assert provider._api_version == "2025-01-01-preview"


def test_azure_adapter_sends_strict_schema_and_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == (
            "/openai/deployments/fixture-gpt-deployment/chat/completions"
        )
        assert request.url.params["api-version"] == "2024-10-21"
        assert request.headers["api-key"] == "test-key"
        body = json.loads(request.content)
        assert body["temperature"] == 0
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert ALTERNATE_TEXT in body["messages"][1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": EXTRACTION.model_dump_json()
                        }
                    }
                ]
            },
        )

    provider = AzureOpenAIItineraryProvider(
        endpoint="https://azure.test",
        api_key="test-key",
        deployment="fixture-gpt-deployment",
        transport=httpx.MockTransport(handler),
    )

    assert provider.extract_itinerary(ALTERNATE_TEXT) == EXTRACTION


def test_azure_adapter_fails_closed_without_deployment() -> None:
    provider = AzureOpenAIItineraryProvider(
        endpoint="https://azure.test",
        api_key="test-key",
        deployment="",
    )

    with pytest.raises(ItineraryLlmNotConfiguredError):
        provider.extract_itinerary(ALTERNATE_TEXT)


def test_llm_fallback_recovers_an_alternate_ticket_layout() -> None:
    document = (PDF_ROOT / "redacted_ambiguous_scan.pdf").read_bytes()
    provider = FixtureItineraryLlm()
    outcome = ParseOutcome.model_validate(
        run_document_flow(
            document,
            metadata(document),
            AlternateTextOcr(),
            provider,
            "fallback",
        )
    )

    assert provider.calls == [ALTERNATE_TEXT]
    assert outcome.status == "parsed"
    assert outcome.itinerary is not None
    assert outcome.itinerary.confirmation_codes == ["ZXCV12"]
    assert outcome.itinerary.legs[0].flight_number == "NB204"
    assert outcome.itinerary.legs[0].scheduled_departure_at == (
        "2026-09-15T07:00:00Z"
    )
    assert outcome.itinerary.legs[0].scheduled_arrival_at == (
        "2026-09-15T08:15:00Z"
    )
    assert outcome.orchestration["llm_calls"] == 1
    assert outcome.orchestration["llm"]["result"] == "parsed"


def test_deterministic_success_never_calls_llm() -> None:
    document = (PDF_ROOT / "synthetic_direct_eticket.pdf").read_bytes()
    provider = FixtureItineraryLlm()
    outcome = ParseOutcome.model_validate(
        run_document_flow(
            document,
            metadata(document, suffix="deterministic"),
            NeverOcr(),
            provider,
            "fallback",
        )
    )

    assert outcome.status == "parsed"
    assert provider.calls == []
    assert outcome.orchestration["llm_calls"] == 0


@pytest.mark.parametrize("failure", ["missing_evidence", "low_confidence"])
def test_llm_fallback_rejects_unsupported_or_uncertain_output(
    failure: str,
) -> None:
    document = (PDF_ROOT / "redacted_ambiguous_scan.pdf").read_bytes()
    value = EXTRACTION.model_dump(mode="json")
    if failure == "missing_evidence":
        value["legs"][0]["flight_number_evidence"] = "Flight: MADEUP999"
    else:
        value["legs"][0]["confidence"] = 0.50
    provider = FixtureItineraryLlm(
        LlmItineraryExtraction.model_validate(value)
    )

    outcome = ParseOutcome.model_validate(
        run_document_flow(
            document,
            metadata(document, suffix=failure.replace("_", "-")),
            AlternateTextOcr(),
            provider,
            "fallback",
        )
    )

    assert outcome.status == "review_required"
    assert outcome.itinerary is None
    assert outcome.review is not None
    assert outcome.review["reason_codes"][0] == "LLM_EXTRACTION_FAILED"
    assert outcome.orchestration["llm_calls"] == 1


def test_llm_trace_input_never_contains_ticket_text_or_raw_authority() -> None:
    document = b"synthetic"
    document_metadata = metadata(document, suffix="trace")
    base = parse_extracted_text(ALTERNATE_TEXT, document_metadata)
    trace_value = llm_trace_input(
        ALTERNATE_TEXT,
        document_metadata,
        FixtureItineraryLlm(),
        base,
    )
    rendered = json.dumps(trace_value)

    assert "NB204" not in rendered
    assert "ZXCV12" not in rendered
    assert ALTERNATE_TEXT not in rendered
    assert document_metadata.trip_id not in rendered
    assert trace_value["source"]["character_count"] == len(ALTERNATE_TEXT)


def test_invalid_provider_response_raises_safe_error() -> None:
    provider = AzureOpenAIItineraryProvider(
        endpoint="https://azure.test",
        api_key="test-key",
        deployment="fixture-gpt-deployment",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"choices": [{"message": {"content": "not-json"}}]}
            )
        ),
    )

    with pytest.raises(ItineraryLlmError):
        provider.extract_itinerary(ALTERNATE_TEXT)
