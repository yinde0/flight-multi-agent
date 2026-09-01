from __future__ import annotations

import base64
import binascii
import os
import secrets
import threading

from pathlib import Path

import httpx

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import Response

from flight_agent.disruption_explanation import (
    AzureOpenAIDisruptionExplanationProvider,
    DisruptionExplanationRequest,
)
from flight_agent.eval_reasoning import CrewAIEvalReasoner

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
from flight_agent.itinerary_llm import AzureOpenAIItineraryProvider
from flight_agent.ocr import MistralOcrProvider
from flight_agent.telemetry import install_trace_middleware
from flight_agent.travel_tools_auth import (
    COMMUNICATION_SCOPE,
    DOCUMENT_SCOPE,
    EVAL_SCOPE,
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

# Provider credentials exist only in this network-broker process. Other agents
# call these adapters through authenticated MCP tools.
ocr_provider = MistralOcrProvider.from_environment()
itinerary_llm_provider = AzureOpenAIItineraryProvider.from_environment()
explanation_provider = AzureOpenAIDisruptionExplanationProvider.from_environment()


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


@mcp.tool(
    name="extract_ticket_text",
    description="Extract text from an image-only ticket PDF with Mistral OCR.",
    structured_output=True,
)
def extract_ticket_text(pdf_base64: str, ctx: Context) -> dict[str, object]:
    authorize_tool_call(ctx, DOCUMENT_SCOPE)
    try:
        document = base64.b64decode(pdf_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("pdf_base64 must contain a valid base64 PDF") from error
    maximum = int(os.getenv("MCP_MAX_PDF_BYTES", "15728640"))
    if not document or len(document) > maximum or not document.startswith(b"%PDF"):
        raise ValueError("Document is not a valid PDF or exceeds the MCP size limit")
    extraction = ocr_provider.extract_pdf(document)
    return {
        "text": extraction.text,
        "provider": extraction.provider,
        "model": extraction.model,
        "page_count": extraction.page_count,
    }


@mcp.tool(
    name="extract_itinerary_with_llm",
    description="Extract a schema-constrained itinerary using Azure OpenAI.",
    structured_output=True,
)
def extract_itinerary_with_llm(
    ticket_text: str, ctx: Context
) -> dict[str, object]:
    authorize_tool_call(ctx, DOCUMENT_SCOPE)
    extraction = itinerary_llm_provider.extract_itinerary(ticket_text)
    return {
        "model": itinerary_llm_provider.model_name,
        "extraction": extraction.model_dump(mode="json"),
    }


@mcp.tool(
    name="generate_disruption_explanation",
    description="Generate friendly wording from Eval-approved disruption facts.",
    structured_output=True,
)
def generate_disruption_explanation(
    request: DisruptionExplanationRequest, ctx: Context
) -> dict[str, object]:
    authorize_tool_call(ctx, COMMUNICATION_SCOPE)
    explanation = explanation_provider.explain(request)
    return {
        "model": explanation_provider.model_name,
        "explanation": explanation.model_dump(mode="json"),
    }


@mcp.tool(
    name="review_disruption_decision",
    description=(
        "Run the tool-free CrewAI shadow reviewer; deterministic policy remains authoritative."
    ),
    structured_output=True,
)
def review_disruption_decision(
    candidate: dict,
    policy: dict,
    deterministic_decision: dict,
    ctx: Context,
) -> dict[str, object]:
    authorize_tool_call(ctx, EVAL_SCOPE)
    reasoner = CrewAIEvalReasoner()
    advisory = reasoner.recommend(candidate, policy, deterministic_decision)
    return {
        "model": reasoner.model_name,
        "advisory": advisory.model_dump(mode="json"),
    }


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
                "ocr": type(ocr_provider).__name__,
                "itinerary_llm": type(itinerary_llm_provider).__name__,
                "explanation_llm": type(explanation_provider).__name__,
            },
            "authorization_enabled": (
                os.getenv("TRAVEL_TOOLS_AUTH_ENABLED", "false").lower() == "true"
            ),
        }
    )


@mcp.custom_route("/otel/v1/traces", methods=["POST"])
async def forward_langsmith_traces(request: Request) -> Response:
    """Private OTLP relay so agent tasks never need public internet access."""

    expected = os.getenv("TRAVEL_TOOLS_TELEMETRY_TOKEN", "")
    supplied = request.headers.get("x-travel-tools-token", "")
    if (
        not expected
        and os.getenv("DEPLOYMENT_ENVIRONMENT", "development") != "development"
    ):
        return JSONResponse(
            {"detail": "LangSmith relay authorization is not configured"},
            status_code=503,
        )
    if expected and (
        not supplied or not secrets.compare_digest(expected, supplied)
    ):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    api_key = os.getenv("LANGSMITH_API_KEY", "").strip()
    project = os.getenv("LANGSMITH_PROJECT", "flight-multi-agent").strip()
    endpoint = os.getenv("LANGSMITH_OTEL_ENDPOINT", "").strip()
    if not endpoint:
        endpoint = (
            os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
            .rstrip("/")
            + "/otel/v1/traces"
        )
    if not api_key:
        return JSONResponse(
            {"detail": "LangSmith relay is not configured"}, status_code=503
        )
    body = await request.body()
    if len(body) > int(os.getenv("LANGSMITH_RELAY_MAX_BYTES", "5242880")):
        return JSONResponse(
            {"detail": "OTLP batch exceeds the relay size limit"},
            status_code=413,
        )
    headers = {
        "x-api-key": api_key,
        "Langsmith-Project": project,
        "Content-Type": request.headers.get(
            "content-type", "application/x-protobuf"
        ),
    }
    content_encoding = request.headers.get("content-encoding")
    if content_encoding:
        headers["Content-Encoding"] = content_encoding
    try:
        async with httpx.AsyncClient(
            timeout=float(os.getenv("LANGSMITH_RELAY_TIMEOUT_SECONDS", "10"))
        ) as client:
            response = await client.post(
                endpoint, content=body, headers=headers
            )
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type"),
        )
    except httpx.HTTPError:
        return JSONResponse(
            {"detail": "LangSmith relay unavailable"}, status_code=503
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
