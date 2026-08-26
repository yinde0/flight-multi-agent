from __future__ import annotations

import asyncio
import json

from typing import Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from flight_agent.flight_search_contracts import (
    FlightSearchCommand,
    FlightSearchToolResult,
)


class FlightSearchGateway(Protocol):
    def search_flights(self, command: FlightSearchCommand) -> FlightSearchToolResult: ...


class StreamableHttpFlightSearchMcpClient:
    def __init__(self, url: str) -> None:
        self._url = url

    def search_flights(self, command: FlightSearchCommand) -> FlightSearchToolResult:
        payload = asyncio.run(
            self._call_tool(
                "search_flights",
                {"command": command.model_dump(mode="json")},
            )
        )
        return FlightSearchToolResult.model_validate(payload)

    async def _call_tool(self, name: str, arguments: dict) -> dict:
        async with streamable_http_client(self._url) as (
            read_stream,
            write_stream,
            _,
        ):
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
