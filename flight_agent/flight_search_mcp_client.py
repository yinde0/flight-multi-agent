from __future__ import annotations

import asyncio
import json

from typing import Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import TextContent

from flight_agent.flight_search_contracts import (
    FlightSearchCommand,
    FlightSearchToolResult,
)
from flight_agent.telemetry import trace_headers, traced


class FlightSearchGateway(Protocol):
    def search_flights(self, command: FlightSearchCommand) -> FlightSearchToolResult: ...


class StreamableHttpFlightSearchMcpClient:
    def __init__(self, url: str) -> None:
        self._url = url

    @traced(
        "mcp.search_flights",
        service_name="flight-search-action-service",
        kind="tool",
    )
    def search_flights(self, command: FlightSearchCommand) -> FlightSearchToolResult:
        payload = asyncio.run(
            self._call_tool(
                "search_flights",
                {"command": command.model_dump(mode="json")},
            )
        )
        return FlightSearchToolResult.model_validate(payload)

    async def _call_tool(self, name: str, arguments: dict) -> dict:
        async with create_mcp_http_client(headers=trace_headers()) as http_client:
            async with streamable_http_client(
                self._url, http_client=http_client
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments=arguments)
        if result.isError:
            raise RuntimeError("Flight search MCP tool returned an error")
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
            raise RuntimeError("Flight search MCP tool returned no structured output")
        return payload
