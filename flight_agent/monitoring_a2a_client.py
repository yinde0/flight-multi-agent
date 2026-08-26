from __future__ import annotations

import uuid

from typing import Any, Protocol

import httpx

from flight_agent.monitoring_contracts import (
    MonitoringPollOutcome,
    MonitoringPollRequest,
)


class MonitoringAgentGateway(Protocol):
    async def poll(self, request: MonitoringPollRequest) -> MonitoringPollOutcome: ...


class A2AMonitoringAgentClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def _jsonrpc_url(self, client: httpx.AsyncClient) -> str:
        response = await client.get(
            f"{self._base_url}/.well-known/agent-card.json"
        )
        response.raise_for_status()
        card = response.json()
        skills = {skill.get("id") for skill in card.get("skills", [])}
        if "poll_flight_status" not in skills:
            raise RuntimeError("Monitoring Agent does not advertise poll_flight_status")
        for interface in card.get("supportedInterfaces", []):
            if (
                interface.get("protocolBinding") == "JSONRPC"
                and interface.get("protocolVersion") == "1.0"
            ):
                return str(interface["url"])
        raise RuntimeError("Monitoring Agent has no A2A JSON-RPC 1.0 interface")

    async def poll(self, request: MonitoringPollRequest) -> MonitoringPollOutcome:
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            jsonrpc_url = await self._jsonrpc_url(client)
            request_id = str(uuid.uuid4())
            response = await client.post(
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
                    return MonitoringPollOutcome.model_validate(data)
        raise RuntimeError("A2A response has no structured monitoring artifact")
