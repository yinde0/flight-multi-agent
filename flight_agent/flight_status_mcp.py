from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from flight_agent.flight_status import (
    AviationStackFlightStatusProvider,
    FlightStatusProviderError,
    provider_from_environment,
)
from flight_agent.monitoring_contracts import (
    LiveFlightSample,
    ProviderFlightObservation,
)
from flight_agent.telemetry import install_trace_middleware


provider = provider_from_environment()
mcp = FastMCP(
    "Travel Flight Status",
    instructions=(
        "Read-only flight status access. This server never books, cancels, or "
        "modifies travel."
    ),
    host="0.0.0.0",
    port=8003,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "flight-status-mcp:8003",
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
    replay_key: str | None = None,
) -> ProviderFlightObservation:
    observation = provider.get_flight_status(
        flight_iata=flight_iata.upper(),
        flight_date=flight_date,
        replay_key=replay_key,
    )
    return observation


@mcp.tool(
    name="discover_live_flight_sample",
    description=(
        "Select one usable flight from AviationStack's unfiltered real-time "
        "feed for credential-safe integration testing."
    ),
    structured_output=True,
)
def discover_live_flight_sample(limit: int = 10) -> LiveFlightSample:
    if not isinstance(provider, AviationStackFlightStatusProvider):
        raise FlightStatusProviderError(
            "Live discovery is available only with the AviationStack provider"
        )
    return provider.discover_live_flight_sample(limit=limit)


@mcp.custom_route("/health/live", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    del request
    return JSONResponse({"status": "ok", "provider": type(provider).__name__})


app = mcp.streamable_http_app()
install_trace_middleware(app, service_name="flight-status-mcp")
