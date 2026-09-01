from __future__ import annotations

from flight_agent.disruption_explanation import DisruptionExplanationRequest
from flight_agent import provider_mcp_clients as clients
from flight_agent.travel_tools_auth import (
    COMMUNICATION_SCOPE,
    DOCUMENT_SCOPE,
    EVAL_SCOPE,
)


def test_document_providers_translate_mcp_structured_outputs(monkeypatch):
    calls = []

    async def call(url, name, arguments, scope):
        calls.append((url, name, arguments, scope))
        if name == "extract_ticket_text":
            return {
                "text": "Flight NB204",
                "provider": "mistral",
                "model": "mistral-ocr-latest",
                "page_count": 1,
            }
        return {
            "model": "gpt-4.1-mini",
            "extraction": {
                "can_parse": False,
                "confirmation_codes": [],
                "confirmation_evidence": [],
                "legs": [],
                "reason_codes": ["AMBIGUOUS_DATE"],
            },
        }

    monkeypatch.setattr(clients, "_call_tool", call)
    ocr = clients.StreamableHttpOcrMcpClient("http://tools/mcp").extract_pdf(
        b"%PDF-unit"
    )
    itinerary = clients.StreamableHttpItineraryLlmMcpClient(
        "http://tools/mcp"
    ).extract_itinerary("ambiguous ticket")

    assert ocr.text == "Flight NB204"
    assert itinerary.reason_codes == ["AMBIGUOUS_DATE"]
    assert [value[3] for value in calls] == [DOCUMENT_SCOPE, DOCUMENT_SCOPE]


def test_communication_and_eval_providers_use_their_own_scopes(monkeypatch):
    calls = []

    async def call(url, name, arguments, scope):
        calls.append((url, name, arguments, scope))
        if name == "generate_disruption_explanation":
            return {
                "model": "gpt-4.1-mini",
                "explanation": {
                    "schema_version": "1.0.0",
                    "prompt_version": "disruption-explanation-v1",
                    "message": "Your flight is now delayed by 45 minutes.",
                    "facts_used": ["category", "delay_minutes"],
                    "confidence": 0.98,
                },
            }
        return {
            "model": "mistral-small-latest",
            "advisory": {
                "schema_version": "1.0.0",
                "prompt_version": "eval-advisory-v1",
                "recommended_verdict": "NOTIFY",
                "reason_codes": ["DELAY_THRESHOLD_MET"],
                "confidence": 0.95,
                "rationale": "The deterministic delay threshold was met.",
            },
        }

    monkeypatch.setattr(clients, "_call_tool", call)
    explanation = clients.StreamableHttpDisruptionExplanationMcpClient(
        "http://tools/mcp"
    ).explain(
        DisruptionExplanationRequest(
            category="DELAY",
            verdict="NOTIFY",
            reason_codes=["DELAY_THRESHOLD_MET"],
            delay_minutes=45,
            search_requested=False,
        )
    )
    advisory = clients.StreamableHttpEvalReasonerMcpClient(
        "http://tools/mcp"
    ).recommend(
        {"category": "DELAY"},
        {"policy_version": "1.0.0"},
        {"verdict": "NOTIFY"},
    )

    assert explanation.confidence == 0.98
    assert advisory.recommended_verdict == "NOTIFY"
    assert [value[3] for value in calls] == [COMMUNICATION_SCOPE, EVAL_SCOPE]
