from __future__ import annotations

import asyncio
import json

from typing import Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from flight_agent.monitoring_contracts import ProviderWeatherObservation


class WeatherGateway(Protocol):
    def get_airport_weather(
        self,
        *,
        airport: str,
        target_at: str,
        replay_key: str | None = None,
    ) -> ProviderWeatherObservation: ...


class StreamableHttpWeatherMcpClient:
    """Synchronous MCP gateway used inside the CrewAI worker thread."""

    def __init__(self, url: str) -> None:
        self._url = url

    def get_airport_weather(
        self,
        *,
        airport: str,
        target_at: str,
        replay_key: str | None = None,
    ) -> ProviderWeatherObservation:
        payload = asyncio.run(
            self._call_tool(
                "get_airport_weather",
                {
                    "airport": airport,
                    "target_at": target_at,
                    "replay_key": replay_key,
                },
            )
        )
        return ProviderWeatherObservation.model_validate(payload)

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
            raise RuntimeError("Weather MCP tool returned an error")
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
            raise RuntimeError("Weather MCP tool returned no structured output")
        return payload
