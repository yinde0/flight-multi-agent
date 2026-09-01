from __future__ import annotations

import os
import secrets
import threading

from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from flight_agent.flight_search import (
    DisabledFlightSearchProvider,
    DuffelFlightSearchProvider,
    ReplayFlightSearchProvider,
    run_provider_search,
)
from flight_agent.flight_search_contracts import (
    FlightSearchCommand,
    FlightSearchToolResult,
)
from flight_agent.monitoring_contracts import (
    LiveFlightSample,
    ProviderFlightObservation,
    ProviderWeatherObservation,
)
from flight_agent.flight_status import (
    AviationStackFlightStatusProvider,
    FlightStatusProviderError,
    provider_from_environment as flight_status_provider_from_environment,
)
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
from flight_agent.travel_tools_auth import (
    MONITOR_SCOPE,
    NOTIFICATION_SCOPE,
    SEARCH_SCOPE,
    authorize_tool_call,
)
from flight_agent.weather import provider_from_environment as weather_provider_from_environment


ROOT = Path(__file__).resolve().parents[1]
flight_status_provider = flight_status_provider_from_environment()
weather_provider = weather_provider_from_environment()

flight_search_provider_name = os.getenv("FLIGHT_SEARCH_PROVIDER", "duffel").lower()
if flight_search_provider_name == "replay":
    flight_search_provider = ReplayFlightSearchProvider(
        os.getenv(
            "FLIGHT_SEARCH_REPLAY_FIXTURE",
            str(ROOT / "travel_eval" / "fixtures" / "search" / "vertical_06_options.json"),
        )
    )
elif flight_search_provider_name == "duffel":
    flight_search_provider = DuffelFlightSearchProvider(
        token=os.getenv("DUFFEL_TOKEN", ""),
        base_url=os.getenv("DUFFEL_BASE_URL", "https://api.duffel.com"),
        timeout_seconds=float(os.getenv("DUFFEL_TIMEOUT_SECONDS", "30")),
        supplier_timeout_ms=int(os.getenv("DUFFEL_SUPPLIER_TIMEOUT_MS", "10000")),
        maximum_offers=int(os.getenv("DUFFEL_MAX_OFFERS", "100")),
    )
elif flight_search_provider_name == "disabled":
    flight_search_provider = DisabledFlightSearchProvider()
else:
    raise RuntimeError(f"Unsupported flight search provider: {flight_search_provider_name}")

notification_provider_name = os.getenv("NOTIFICATION_PROVIDER", "recording").lower()
if notification_provider_name == "recording":
    notification_provider = RecordingNotificationProvider()
elif notification_provider_name == "twilio":
    notification_provider = TwilioNotificationProvider.from_environment()
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


notification_failure_gate = NotificationFailureGate()


mcp = FastMCP(
    "Travel Tools",
    instructions=(
        "Provide flight status, airport weather, read-only flight search, and "
        "post-Eval notification tools. Flight search never books or takes payment, "
        "and notification requires the approved notification action path."
    ),
    host="0.0.0.0",
    port=8003,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "travel-tools-mcp:8003",
            "localhost:8003",
            "127.0.0.1:8003",
            "testserver",
        ]
    ),
)


@mcp.tool(
    name="get_flight_status",
    description=(
        "Return one normalized observation for an IATA flight number and date. "
        "replay_key is ignored by the live provider."
    ),
    structured_output=True,
)
def get_flight_status(
    flight_iata: str,
    flight_date: str,
    ctx: Context,
    replay_key: str | None = None,
) -> ProviderFlightObservation:
    authorize_tool_call(ctx, MONITOR_SCOPE)
    return flight_status_provider.get_flight_status(
        flight_iata=flight_iata,
        flight_date=flight_date,
        replay_key=replay_key,
    )


@mcp.tool(
    name="discover_live_flight_sample",
    description=(
        "Select one usable flight from AviationStack's unfiltered real-time "
        "feed for credential-safe integration testing."
    ),
    structured_output=True,
)
def discover_live_flight_sample(ctx: Context, limit: int = 10) -> LiveFlightSample:
    authorize_tool_call(ctx, MONITOR_SCOPE)
    if not isinstance(flight_status_provider, AviationStackFlightStatusProvider):
        raise FlightStatusProviderError(
            "Live discovery is available only with the AviationStack provider"
        )
    return flight_status_provider.discover_live_flight_sample(limit=limit)


@mcp.tool(
    name="get_airport_weather",
    description=(
        "Return the normalized forecast nearest a flight time for an IATA airport. "
        "replay_key is ignored by the live provider."
    ),
    structured_output=True,
)
def get_airport_weather(
    airport: str,
    target_at: str,
    ctx: Context,
    replay_key: str | None = None,
) -> ProviderWeatherObservation:
    authorize_tool_call(ctx, MONITOR_SCOPE)
    return weather_provider.get_airport_weather(
        airport=airport,
        target_at=target_at,
        replay_key=replay_key,
    )


@mcp.tool(
    name="search_flights",
    description=(
        "Return expiring, read-only flight offers with explicit test/live evidence; "
        "never make a booking claim."
    ),
    structured_output=True,
)
def search_flights(
    command: FlightSearchCommand, ctx: Context
) -> FlightSearchToolResult:
    authorize_tool_call(ctx, SEARCH_SCOPE)
    return run_provider_search(command, flight_search_provider)


@mcp.tool(
    name="send_notification",
    description="Send an idempotent notification only after a verified Eval approval.",
    structured_output=True,
)
def send_notification(
    command: NotificationCommand, ctx: Context
) -> NotificationReceipt | NotificationSubmissionFailure:
    authorize_tool_call(ctx, NOTIFICATION_SCOPE)
    if notification_failure_gate.enabled():
        raise RuntimeError("Injected notification capability outage")
    try:
        return notification_provider.send(command)
    except NotificationSubmissionError as error:
        return error.failure


@mcp.custom_route("/health/live", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    del request
    return JSONResponse(
        {
            "status": "ok",
            "providers": {
                "flight_status": type(flight_status_provider).__name__,
                "weather": type(weather_provider).__name__,
                "flight_search": flight_search_provider.name,
                "notification": notification_provider_name,
            },
            "authorization_enabled": (
                os.getenv("TRAVEL_TOOLS_AUTH_ENABLED", "false").lower() == "true"
            ),
        }
    )


@mcp.custom_route("/v1/reliability/audit", methods=["GET"])
async def reliability_audit(request: Request) -> JSONResponse:
    del request
    if os.getenv("RELIABILITY_AUDIT_ENABLED", "false").lower() != "true":
        return JSONResponse({"detail": "Not found"}, status_code=404)
    search_audit = getattr(flight_search_provider, "audit", None)
    notification_audit = getattr(notification_provider, "audit", None)
    return JSONResponse(
        {
            "flight_search": (
                search_audit()
                if callable(search_audit)
                else {"provider_call_count": None}
            ),
            "notification": {
                **(
                    notification_audit()
                    if callable(notification_audit)
                    else {"provider_call_count": None}
                ),
                "failure_mode_enabled": notification_failure_gate.enabled(),
            },
        }
    )


@mcp.custom_route("/v1/operations/notification/failure-mode", methods=["POST"])
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
    notification_failure_gate.set_enabled(enabled)
    return JSONResponse(
        {"failure_mode_enabled": notification_failure_gate.enabled()}
    )


app = mcp.streamable_http_app()
install_trace_middleware(app, service_name="travel-tools-mcp")
