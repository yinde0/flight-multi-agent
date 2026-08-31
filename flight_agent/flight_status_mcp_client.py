from __future__ import annotations

import asyncio
import json

from typing import Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import TextContent

from flight_agent.monitoring_contracts import (
    LiveFlightSample,
    ProviderFlightObservation,
)
from flight_agent.telemetry import trace_headers, traced
from flight_agent.mcp_trace_views import flight_status_input, flight_status_output, live_sample_output


class FlightStatusGateway(Protocol):
    def get_flight_status(
        self,
        *,
        flight_iata: str,
        flight_date: str,
        replay_key: str | None = None,
    ) -> ProviderFlightObservation: ...


class StreamableHttpFlightStatusMcpClient:
    """MCP client used by the Monitoring Agent inside its CrewAI worker thread."""

    def __init__(self, url: str) -> None:
        self._url = url

    @traced(
        "mcp.get_flight_status",
        service_name="monitor-agent",
        kind="tool",
        content_input=flight_status_input,
        content_output=flight_status_output,
    )
    def get_flight_status(
        self,
        *,
        flight_iata: str,
        flight_date: str,
        replay_key: str | None = None,
    ) -> ProviderFlightObservation:
        payload = asyncio.run(
            self._call_tool(
                "get_flight_status",
                {
                    "flight_iata": flight_iata,
                    "flight_date": flight_date,
                    "replay_key": replay_key,
                },
            )
        )
        return ProviderFlightObservation.model_validate(payload)

    @traced(
        "mcp.discover_live_flight_sample",
        service_name="monitor-agent",
        kind="tool",
        content_input=lambda self, *, limit=10: {"limit": limit},
        content_output=live_sample_output,
    )
    def discover_live_flight_sample(self, *, limit: int = 10) -> LiveFlightSample:
        payload = asyncio.run(
            self._call_tool("discover_live_flight_sample", {"limit": limit})
        )
        return LiveFlightSample.model_validate(payload)

    async def _call_tool(self, name: str, arguments: dict) -> dict:
        async with create_mcp_http_client(headers=trace_headers()) as http_client:
            async with streamable_http_client(
                self._url, http_client=http_client
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments=arguments)

        if result.isError:
            raise RuntimeError("Flight-status MCP tool returned an error")
        payload = result.structuredContent
        if not isinstance(payload, dict):
            for part in result.content:
                if isinstance(part, TextContent):
                    try:
                        candidate = json.loads(part.text)
                    except ValueError:
                        continue
                    if isinstance(candidate, dict):
                        payload = candidate
                        break
        if not isinstance(payload, dict):
            raise RuntimeError("Flight-status MCP tool returned no structured output")
        return payload
