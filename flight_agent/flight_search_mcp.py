from __future__ import annotations

import os

from pathlib import Path

from mcp.server.fastmcp import FastMCP
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
from flight_agent.telemetry import install_trace_middleware


ROOT = Path(__file__).resolve().parents[1]
provider_name = os.getenv("FLIGHT_SEARCH_PROVIDER", "duffel").lower()
if provider_name == "replay":
    provider = ReplayFlightSearchProvider(
        os.getenv(
            "FLIGHT_SEARCH_REPLAY_FIXTURE",
            str(ROOT / "travel_eval" / "fixtures" / "search" / "vertical_06_options.json"),
        )
    )
elif provider_name == "duffel":
    provider = DuffelFlightSearchProvider(
        token=os.getenv("DUFFEL_TOKEN", ""),
        base_url=os.getenv("DUFFEL_BASE_URL", "https://api.duffel.com"),
        timeout_seconds=float(os.getenv("DUFFEL_TIMEOUT_SECONDS", "30")),
        supplier_timeout_ms=int(os.getenv("DUFFEL_SUPPLIER_TIMEOUT_MS", "10000")),
        maximum_offers=int(os.getenv("DUFFEL_MAX_OFFERS", "100")),
    )
elif provider_name == "disabled":
    provider = DisabledFlightSearchProvider()
else:
    raise RuntimeError(f"Unsupported flight search provider: {provider_name}")

mcp = FastMCP(
    "Travel Flight Search",
    instructions=(
        "Perform read-only offer search only after NOTIFY_AND_SEARCH approval. "
        "Never book, hold, cancel, exchange, or take payment."
    ),
    host="0.0.0.0",
    port=8009,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "flight-search-mcp:8009",
            "localhost:8009",
            "127.0.0.1:8009",
            "testserver",
        ]
    ),
)


@mcp.tool(
    name="search_flights",
    description=(
        "Return expiring, read-only flight offers with explicit test/live evidence; "
        "never make a booking claim."
    ),
    structured_output=True,
)
def search_flights(command: FlightSearchCommand) -> FlightSearchToolResult:
    return run_provider_search(command, provider)


@mcp.custom_route("/health/live", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    del request
    return JSONResponse({"status": "ok", "provider": provider.name})


@mcp.custom_route("/v1/reliability/audit", methods=["GET"])
async def reliability_audit(request: Request) -> JSONResponse:
    del request
    if os.getenv("RELIABILITY_AUDIT_ENABLED", "false").lower() != "true":
        return JSONResponse({"detail": "Not found"}, status_code=404)
    audit = getattr(provider, "audit", None)
    return JSONResponse(
        audit() if callable(audit) else {"provider_call_count": None}
    )


app = mcp.streamable_http_app()
install_trace_middleware(app, service_name="flight-search-mcp")
