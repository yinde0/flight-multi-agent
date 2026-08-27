from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from flight_agent.monitoring_contracts import ProviderWeatherObservation
from flight_agent.telemetry import install_trace_middleware
from flight_agent.weather import provider_from_environment


provider = provider_from_environment()
mcp = FastMCP(
    "Travel Airport Weather",
    instructions=(
        "Read-only airport forecast access. Weather is evidence for the Eval "
        "Agent and never authorizes a notification or travel change."
    ),
    host="0.0.0.0",
    port=8006,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "weather-mcp:8006",
            "localhost:8006",
            "127.0.0.1:8006",
            "testserver",
        ]
    ),
)


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
    replay_key: str | None = None,
) -> ProviderWeatherObservation:
    return provider.get_airport_weather(
        airport=airport.upper(),
        target_at=target_at,
        replay_key=replay_key,
    )


@mcp.custom_route("/health/live", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    del request
    return JSONResponse({"status": "ok", "provider": type(provider).__name__})


app = mcp.streamable_http_app()
install_trace_middleware(app, service_name="weather-mcp")
