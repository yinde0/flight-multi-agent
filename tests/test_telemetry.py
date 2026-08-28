from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from flight_agent import telemetry
from flight_agent.flight_status_mcp_client import (
    StreamableHttpFlightStatusMcpClient,
)
from flight_agent.notification_mcp_client import (
    StreamableHttpNotificationMcpClient,
)
from flight_agent.weather_mcp_client import StreamableHttpWeatherMcpClient


class FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.name: str | None = None

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def update_name(self, name: str) -> None:
        self.name = name


def test_content_capture_requires_explicit_development_mode(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_TRACE_CONTENT_ENABLED", "true")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "production")
    assert telemetry.development_content_capture_enabled() is False

    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "development")
    assert telemetry.development_content_capture_enabled() is True

    monkeypatch.setenv("OTEL_TRACE_CONTENT_ENABLED", "false")
    assert telemetry.development_content_capture_enabled() is False


def test_development_content_maps_to_langsmith_input_and_output(
    monkeypatch,
) -> None:
    monkeypatch.setattr(telemetry, "_content_capture_enabled", True)
    span = FakeSpan()

    telemetry._set_span_content(
        span,
        "input",
        {"instruction": "Parse the synthetic itinerary", "trip": "synthetic"},
    )
    telemetry._set_span_content(
        span,
        "output",
        {"status": "parsed", "flight_number": "NB204"},
    )

    assert "Parse the synthetic itinerary" in str(
        span.attributes["gen_ai.prompt.0.content"]
    )
    assert "NB204" in str(span.attributes["gen_ai.completion.0.content"])
    assert (
        span.attributes["langsmith.metadata.content_capture"]
        == "development_explicit"
    )


def test_content_is_not_attached_when_capture_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "_content_capture_enabled", False)
    span = FakeSpan()

    telemetry._set_span_content(span, "input", {"secret": "must-not-appear"})

    assert span.attributes == {}


def _test_span_context() -> SpanContext:
    return SpanContext(
        trace_id=int("1234567890abcdef1234567890abcdef", 16),
        span_id=int("1234567890abcdef", 16),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )


def test_w3c_trace_headers_round_trip_without_baggage() -> None:
    token = otel_context.attach(
        trace.set_span_in_context(NonRecordingSpan(_test_span_context()))
    )
    try:
        headers = telemetry.trace_headers({"baggage": "traveler=must-not-propagate"})
    finally:
        otel_context.detach(token)

    assert headers == {
        "traceparent": (
            "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
        )
    }
    with telemetry.extracted_trace_context(headers):
        assert telemetry.current_trace_id() == "1234567890abcdef1234567890abcdef"


def test_http_middleware_continues_inbound_trace_and_returns_trace_id() -> None:
    app = FastAPI()
    telemetry.install_trace_middleware(app, service_name="trace-test-service")

    @app.get("/probe")
    async def probe() -> dict[str, str | None]:
        return {"trace_id": telemetry.current_trace_id()}

    response = TestClient(app).get(
        "/probe",
        headers={
            "traceparent": (
                "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
            )
        },
    )

    assert response.status_code == 200
    assert response.json()["trace_id"] == "1234567890abcdef1234567890abcdef"
    assert response.headers["x-trace-id"] == "1234567890abcdef1234567890abcdef"


def test_http_middleware_supports_plain_starlette_app() -> None:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(_request):
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/health", health)])
    telemetry.install_trace_middleware(app, service_name="starlette-trace-test")
    response = TestClient(app).get(
        "/health",
        headers={
            "traceparent": (
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
            )
        },
    )

    assert response.status_code == 200
    assert response.headers["x-trace-id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_http_middleware_excludes_probes_and_names_real_routes(monkeypatch) -> None:
    spans: list[FakeSpan] = []

    @contextmanager
    def fake_trace_operation(*_args, **_kwargs):
        span = FakeSpan()
        spans.append(span)
        yield span

    monkeypatch.setattr(telemetry, "trace_operation", fake_trace_operation)
    monkeypatch.setattr(telemetry, "_content_capture_enabled", True)

    app = FastAPI()
    telemetry.install_trace_middleware(app, service_name="route-name-test")

    @app.get("/health/live")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/trips/{trip_id}/check")
    async def check_trip(trip_id: str) -> dict[str, str]:
        return {"trip_id": trip_id}

    client = TestClient(app)
    assert client.get("/health/live").status_code == 200
    assert spans == []

    response = client.post("/v1/trips/trip-synthetic/check")

    assert response.status_code == 200
    assert len(spans) == 1
    assert spans[0].name == "POST /v1/trips/{trip_id}/check"
    assert spans[0].attributes["http.route"] == "/v1/trips/{trip_id}/check"
    assert "status_code" in str(
        spans[0].attributes["gen_ai.completion.0.content"]
    )


def test_langsmith_agent_root_mode_hides_other_http_routes(monkeypatch) -> None:
    spans: list[tuple[str, FakeSpan]] = []

    @contextmanager
    def fake_trace_operation(operation, **_kwargs):
        span = FakeSpan()
        spans.append((operation, span))
        yield span

    monkeypatch.setenv("OTEL_HTTP_TRACE_MODE", "agent_roots")
    monkeypatch.setattr(telemetry, "trace_operation", fake_trace_operation)

    app = FastAPI()
    telemetry.install_trace_middleware(app, service_name="travel-api")

    @app.post("/v1/trips/activate")
    async def activate() -> dict[str, str]:
        return {"status": "activated"}

    @app.post("/v1/internal/post")
    async def internal_post() -> dict[str, str]:
        return {"status": "ok"}

    client = TestClient(app)
    assert client.post("/v1/internal/post").status_code == 200
    assert spans == []
    assert client.post("/v1/trips/activate").status_code == 200
    assert [operation for operation, _span in spans] == [
        "agent.orchestrator.trip_pipeline"
    ]
    assert spans[0][1].name is None


def test_http_trace_off_still_propagates_inbound_context(monkeypatch) -> None:
    spans: list[FakeSpan] = []

    @contextmanager
    def fake_trace_operation(*_args, **_kwargs):
        span = FakeSpan()
        spans.append(span)
        yield span

    monkeypatch.setenv("OTEL_HTTP_TRACE_MODE", "off")
    monkeypatch.setattr(telemetry, "trace_operation", fake_trace_operation)

    app = FastAPI()
    telemetry.install_trace_middleware(app, service_name="internal-service")

    @app.post("/work")
    async def work() -> dict[str, str | None]:
        return {"trace_id": telemetry.current_trace_id()}

    response = TestClient(app).post(
        "/work",
        headers={
            "traceparent": (
                "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
            )
        },
    )

    assert spans == []
    assert response.json()["trace_id"] == "1234567890abcdef1234567890abcdef"
    assert response.headers["x-trace-id"] == "1234567890abcdef1234567890abcdef"


def test_concrete_mcp_client_operations_are_traced() -> None:
    assert hasattr(
        StreamableHttpFlightStatusMcpClient.get_flight_status, "__wrapped__"
    )
    assert hasattr(
        StreamableHttpWeatherMcpClient.get_airport_weather, "__wrapped__"
    )
    assert hasattr(
        StreamableHttpNotificationMcpClient.send_notification, "__wrapped__"
    )
