from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from flight_agent.notification import RecordingNotificationProvider
from flight_agent.notification_contracts import (
    NotificationCommand,
    NotificationReceipt,
)


provider = RecordingNotificationProvider()
mcp = FastMCP(
    "Travel Notification",
    instructions=(
        "Accept only notification commands carrying a non-suppressed Eval approval. "
        "This slice records delivery and cannot contact an external recipient."
    ),
    host="0.0.0.0",
    port=8007,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "notification-mcp:8007",
            "localhost:8007",
            "127.0.0.1:8007",
            "testserver",
        ]
    ),
)


@mcp.tool(
    name="send_notification",
    description=(
        "Record an idempotent notification delivery after a verified Eval approval."
    ),
    structured_output=True,
)
def send_notification(command: NotificationCommand) -> NotificationReceipt:
    return provider.send(command)


@mcp.custom_route("/health/live", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    del request
    return JSONResponse({"status": "ok", "provider": "recording"})


app = mcp.streamable_http_app()
