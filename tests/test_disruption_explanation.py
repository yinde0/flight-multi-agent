from __future__ import annotations

import json

import httpx

from fastapi.testclient import TestClient

from flight_agent.communication_a2a import create_communication_agent_app
from flight_agent.communication_a2a_client import A2ACommunicationAgentClient
from flight_agent.disruption_explanation import (
    AzureOpenAIDisruptionExplanationProvider,
    DisruptionExplanation,
    DisruptionExplanationError,
    DisruptionExplanationRequest,
    ModelDisruptionExplanation,
    explain_disruption,
    explanation_provider_from_environment,
    explanation_trace_input,
)


def delay_request() -> DisruptionExplanationRequest:
    return DisruptionExplanationRequest(
        category="DELAY",
        verdict="NOTIFY",
        reason_codes=["DELAY_NOTIFY_THRESHOLD"],
        delay_minutes=45,
        weather_risk_level="severe",
        corroborated_by_weather=True,
        search_requested=False,
    )


class StaticProvider:
    provider_name = "azure_openai"
    model_name = "fixture-gpt-deployment"

    def __init__(self, message: str, *, confidence: float = 0.98) -> None:
        self.message = message
        self.confidence = confidence

    def explain(self, request):
        del request
        return ModelDisruptionExplanation(
            message=self.message,
            facts_used=["category", "delay_minutes"],
            confidence=self.confidence,
        )


class FailingProvider:
    provider_name = "azure_openai"
    model_name = "fixture-gpt-deployment"

    def explain(self, request):
        del request
        raise DisruptionExplanationError("simulated provider failure")


def test_explainer_accepts_existing_chat_environment_aliases(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_ENDPOINT", "https://azure.test")
    monkeypatch.setenv("AZURE_API_KEY", "test-key")
    monkeypatch.setenv("CHAT_DEPLOYMENT", "chat-gpt-4-1-mini")
    monkeypatch.setenv("CHAT_API_VERSION", "2025-01-01-preview")
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)

    provider = AzureOpenAIDisruptionExplanationProvider.from_environment()

    assert provider.model_name == "chat-gpt-4-1-mini"
    assert provider._api_version == "2025-01-01-preview"


def test_auto_mode_enables_only_complete_azure_configuration(monkeypatch) -> None:
    monkeypatch.setenv("DISRUPTION_EXPLANATION_MODE", "auto")
    monkeypatch.setenv("AZURE_ENDPOINT", "https://azure.test")
    monkeypatch.setenv("AZURE_API_KEY", "test-key")
    monkeypatch.setenv("CHAT_DEPLOYMENT", "chat-gpt-4-1-mini")
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)

    provider = explanation_provider_from_environment()
    assert provider is not None
    assert provider.model_name == "chat-gpt-4-1-mini"

    monkeypatch.delenv("CHAT_DEPLOYMENT")
    assert explanation_provider_from_environment() is None


def test_azure_adapter_sends_strict_schema_and_parses_friendly_message() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert request.url.path.endswith(
            "/openai/deployments/fixture-gpt-deployment/chat/completions"
        )
        assert request.url.params["api-version"] == "2024-10-21"
        assert request.headers["api-key"] == "test-key"
        assert payload["response_format"]["type"] == "json_schema"
        schema = payload["response_format"]["json_schema"]
        assert schema["name"] == "disruption_explanation"
        assert schema["strict"] is True
        assert set(schema["schema"]["required"]) == set(
            schema["schema"]["properties"]
        )
        assert all(
            "default" not in definition
            for definition in schema["schema"]["properties"].values()
        )
        evidence = json.loads(payload["messages"][1]["content"])
        assert evidence["delay_minutes"] == 45
        assert "trip_id" not in evidence
        assert "traveler" not in payload["messages"][1]["content"]
        model_result = ModelDisruptionExplanation(
            message=(
                "Your flight is now delayed by 45 minutes. "
                "We'll keep watching for further changes."
            ),
            facts_used=["category", "delay_minutes"],
            confidence=0.98,
        )
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": model_result.model_dump_json(),
                        }
                    }
                ]
            },
        )

    provider = AzureOpenAIDisruptionExplanationProvider(
        endpoint="https://azure.test",
        api_key="test-key",
        deployment="fixture-gpt-deployment",
        transport=httpx.MockTransport(handler),
    )

    result = explain_disruption(delay_request(), provider)

    assert result.status == "generated"
    assert result.source == "azure_openai"
    assert "45 minutes" in result.message
    assert len(requests) == 1


def test_unsupported_model_claim_falls_back_without_blocking_notification() -> None:
    result = explain_disruption(
        delay_request(),
        StaticProvider(
            "Your flight is delayed by 45 minutes and we have rebooked a new flight."
        ),
    )

    assert result.status == "fallback"
    assert result.source == "deterministic"
    assert result.error_code == "EXPLANATION_LLM_UNSAFE"
    assert result.message == "Your flight is now delayed by 45 minutes."


def test_model_outage_uses_deterministic_friendly_message() -> None:
    result = explain_disruption(delay_request(), FailingProvider())

    assert result.status == "fallback"
    assert result.error_code == "EXPLANATION_LLM_FAILED"
    assert result.message == "Your flight is now delayed by 45 minutes."


def test_trace_input_contains_operational_facts_but_no_customer_identifiers() -> None:
    trace_input = explanation_trace_input(
        delay_request(), StaticProvider("Your flight is delayed by 45 minutes.")
    )
    rendered = json.dumps(trace_input)

    assert trace_input["facts"]["delay_minutes"] == 45
    assert trace_input["contains_traveler_pii"] is False
    for forbidden in ("trip_id", "traveler_ref", "phone", "confirmation"):
        assert forbidden not in rendered


def test_communication_agent_advertises_narrow_non_action_skill() -> None:
    app = create_communication_agent_app(resolve_environment=False)
    card = TestClient(app).get("/.well-known/agent-card.json")

    assert card.status_code == 200
    assert card.json()["skills"][0]["id"] == "explain_confirmed_disruption"


def test_a2a_client_validates_card_and_structured_artifact() -> None:
    result = DisruptionExplanation(
        message="Your flight is now delayed by 45 minutes.",
        status="generated",
        source="azure_openai",
        model="fixture-gpt-deployment",
        confidence=0.98,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={
                    "skills": [{"id": "explain_confirmed_disruption"}],
                    "supportedInterfaces": [
                        {
                            "protocolBinding": "JSONRPC",
                            "protocolVersion": "1.0",
                            "url": "http://communication.test/a2a/jsonrpc",
                        }
                    ],
                },
            )
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "task": {
                        "artifacts": [
                            {"parts": [{"data": result.model_dump(mode="json")}]}
                        ]
                    }
                },
            },
        )

    client = A2ACommunicationAgentClient(
        "http://communication.test",
        transport=httpx.MockTransport(handler),
    )

    assert client.explain(delay_request()) == result
