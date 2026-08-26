from __future__ import annotations

import asyncio
import json

from typing import Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from flight_agent.notification_contracts import (
    NotificationCommand,
    NotificationReceipt,
)


class NotificationGateway(Protocol):
    def send_notification(
        self, command: NotificationCommand
    ) -> NotificationReceipt: ...


class StreamableHttpNotificationMcpClient:
    def __init__(self, url: str) -> None:
        self._url = url

    def send_notification(
        self, command: NotificationCommand
    ) -> NotificationReceipt:
        payload = asyncio.run(
            self._call_tool(
                "send_notification",
                {"command": command.model_dump(mode="json")},
            )
        )
        return NotificationReceipt.model_validate(payload)

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
            raise RuntimeError("Notification MCP tool returned an error")
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
            raise RuntimeError("Notification MCP tool returned no structured output")
        return payload
