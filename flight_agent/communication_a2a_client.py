from __future__ import annotations

import uuid

from typing import Any, Protocol

import httpx

from flight_agent.disruption_explanation import (
    DisruptionExplanation,
    DisruptionExplanationRequest,
)
from flight_agent.telemetry import trace_headers


class CommunicationAgentGateway(Protocol):
    def explain(
        self, request: DisruptionExplanationRequest
    ) -> DisruptionExplanation: ...


class A2ACommunicationAgentClient:
    """Synchronous A2A client used by the durable notification consumer."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 35.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def _jsonrpc_url(self, client: httpx.Client) -> str:
        response = client.get(f"{self._base_url}/.well-known/agent-card.json")
        response.raise_for_status()
        card = response.json()
        skills = {skill.get("id") for skill in card.get("skills", [])}
        if "explain_confirmed_disruption" not in skills:
            raise RuntimeError(
                "Communication Agent does not advertise explain_confirmed_disruption"
            )
        for interface in card.get("supportedInterfaces", []):
            if (
                interface.get("protocolBinding") == "JSONRPC"
                and interface.get("protocolVersion") == "1.0"
            ):
                return str(interface["url"])
        raise RuntimeError("Communication Agent has no A2A JSON-RPC 1.0 interface")

    def explain(
        self, request: DisruptionExplanationRequest
    ) -> DisruptionExplanation:
        with httpx.Client(
            timeout=self._timeout_seconds,
            headers=trace_headers(),
            transport=self._transport,
            trust_env=self._transport is None,
        ) as client:
            jsonrpc_url = self._jsonrpc_url(client)
            request_id = str(uuid.uuid4())
            response = client.post(
                jsonrpc_url,
                headers={"A2A-Version": "1.0"},
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "SendMessage",
                    "params": {
                        "message": {
                            "messageId": str(uuid.uuid4()),
                            "contextId": str(uuid.uuid4()),
                            "role": "ROLE_USER",
                            "parts": [
                                {"data": request.model_dump(mode="json")},
                            ],
                        }
                    },
                },
            )
            response.raise_for_status()
            envelope = response.json()

        if envelope.get("error"):
            raise RuntimeError(f"A2A agent error: {envelope['error']}")
        if envelope.get("id") != request_id:
            raise RuntimeError("A2A response id does not match request id")
        response_result = envelope.get("result", {})
        result = response_result.get("task", response_result)
        for artifact in result.get("artifacts", []):
            parts = artifact.get("parts", artifact.get("content", []))
            for part in parts:
                data: Any = part.get("data")
                if isinstance(data, dict):
                    return DisruptionExplanation.model_validate(data)
        raise RuntimeError("A2A response has no structured explanation artifact")
