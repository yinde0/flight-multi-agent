from __future__ import annotations

import os
import secrets
import threading

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from flight_agent.notification import (
    RecordingNotificationProvider,
    TwilioNotificationProvider,
)
from flight_agent.notification_contracts import (
    NotificationCommand,
    NotificationReceipt,
    NotificationSubmissionFailure,
)
from flight_agent.notification_errors import NotificationSubmissionError
from flight_agent.telemetry import install_trace_middleware


provider_name = os.getenv("NOTIFICATION_PROVIDER", "recording").lower()
if provider_name == "recording":
    provider = RecordingNotificationProvider()
elif provider_name == "twilio":
    provider = TwilioNotificationProvider.from_environment()
else:
    raise RuntimeError("NOTIFICATION_PROVIDER must be recording or twilio")


class NotificationFailureGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = (
            os.getenv("NOTIFICATION_FAILURE_MODE", "false").lower() == "true"
        )

    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled


failure_gate = NotificationFailureGate()
mcp = FastMCP(
    "Travel Notification",
    instructions=(
        "Accept only notification commands carrying a non-suppressed Eval approval. "
        "The configured provider may record delivery or submit a consented SMS."
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
        "Send an idempotent notification only after a verified Eval approval."
    ),
    structured_output=True,
)
def send_notification(command: NotificationCommand) -> NotificationReceipt | NotificationSubmissionFailure:
    if failure_gate.enabled():
        raise RuntimeError("Injected notification capability outage")
    try:
        return provider.send(command)
    except NotificationSubmissionError as error:
        return error.failure


@mcp.custom_route("/health/live", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    del request
    return JSONResponse({"status": "ok", "provider": provider_name})


@mcp.custom_route("/v1/reliability/audit", methods=["GET"])
async def reliability_audit(request: Request) -> JSONResponse:
    del request
    if os.getenv("RELIABILITY_AUDIT_ENABLED", "false").lower() != "true":
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return JSONResponse(
        {**provider.audit(), "failure_mode_enabled": failure_gate.enabled()}
    )


@mcp.custom_route("/v1/operations/failure-mode", methods=["POST"])
async def notification_failure_mode(request: Request) -> JSONResponse:
    if (
        os.getenv("NOTIFICATION_CHAOS_CONTROL_ENABLED", "false").lower()
        != "true"
    ):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    expected = os.getenv("OPS_API_TOKEN", "")
    supplied = request.headers.get("x-ops-token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    payload = await request.json()
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    if not isinstance(enabled, bool):
        return JSONResponse(
            {"detail": "enabled must be a boolean"}, status_code=422
        )
    failure_gate.set_enabled(enabled)
    return JSONResponse({"failure_mode_enabled": failure_gate.enabled()})


app = mcp.streamable_http_app()
install_trace_middleware(app, service_name="notification-mcp")
