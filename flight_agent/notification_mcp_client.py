from __future__ import annotations

import asyncio
import json

from typing import Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import TextContent

from flight_agent.notification_contracts import (
    NotificationCommand,
    NotificationReceipt,
    NotificationSubmissionFailure,
)
from flight_agent.notification_errors import NotificationSubmissionError
from flight_agent.telemetry import set_current_span_content, trace_headers, traced
from flight_agent.mcp_trace_views import notification_input, notification_output


class NotificationGateway(Protocol):
    def send_notification(
        self, command: NotificationCommand
    ) -> NotificationReceipt: ...


class StreamableHttpNotificationMcpClient:
    def __init__(self, url: str) -> None:
        self._url = url

    @traced(
        "mcp.send_notification",
        service_name="notification-action-service",
        kind="tool",
        content_input=notification_input,
        content_output=notification_output,
    )
    def send_notification(
        self, command: NotificationCommand
    ) -> NotificationReceipt:
        payload = asyncio.run(
            self._call_tool(
                "send_notification",
                {"command": command.model_dump(mode="json")},
            )
        )
        # FastMCP wraps a union output in a result object on some SDK versions.
        if isinstance(payload.get("result"), dict):
            payload = payload["result"]
        if payload.get("status") == "failed":
            failure = NotificationSubmissionFailure.model_validate(payload)
            set_current_span_content(output_value=failure.model_dump(mode="json", exclude_none=True))
            raise NotificationSubmissionError(failure)
        return NotificationReceipt.model_validate(payload)

    async def _call_tool(self, name: str, arguments: dict) -> dict:
        async with create_mcp_http_client(headers=trace_headers()) as http_client:
            async with streamable_http_client(
                self._url, http_client=http_client
            ) as (read_stream, write_stream, _):
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
