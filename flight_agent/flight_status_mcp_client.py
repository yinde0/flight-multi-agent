from __future__ import annotations

import asyncio
import json

from typing import Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from flight_agent.monitoring_contracts import ProviderFlightObservation


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

    def get_flight_status(
        self,
        *,
        flight_iata: str,
        flight_date: str,
        replay_key: str | None = None,
    ) -> ProviderFlightObservation:
        return asyncio.run(
            self._call(
                flight_iata=flight_iata,
                flight_date=flight_date,
                replay_key=replay_key,
            )
        )

    async def _call(
        self,
        *,
        flight_iata: str,
        flight_date: str,
        replay_key: str | None,
    ) -> ProviderFlightObservation:
        async with streamable_http_client(self._url) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "get_flight_status",
                    arguments={
                        "flight_iata": flight_iata,
                        "flight_date": flight_date,
                        "replay_key": replay_key,
                    },
                )

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
        return ProviderFlightObservation.model_validate(payload)
