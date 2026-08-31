from __future__ import annotations

from contextlib import contextmanager

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

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
    assert not any(key.startswith("http.") for key in spans[0][1].attributes)


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


@pytest.mark.parametrize("value", [None, "", " \n ", {}, [], {"a": []}, [None, " "]])
def test_empty_values_do_not_claim_to_have_trace_content(monkeypatch, value):
    monkeypatch.setattr(telemetry, "_content_capture_enabled", True)
    span = FakeSpan()
    telemetry._set_span_content(span, "input", value)
    telemetry._set_span_content(span, "output", value)
    assert span.attributes == {}


@pytest.mark.parametrize("value", [0, False, {"options": [], "count": 0}, {"status": "unchanged"}])
def test_real_zero_and_false_results_remain_traceable(monkeypatch, value):
    monkeypatch.setattr(telemetry, "_content_capture_enabled", True)
    span = FakeSpan()
    telemetry._set_span_content(span, "input", value)
    telemetry._set_span_content(span, "output", value)
    assert span.attributes["travel.trace.has_input"] is True
    assert span.attributes["travel.trace.has_output"] is True


@pytest.fixture
def focused_traces(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setenv("OTEL_TRACE_SCOPE", "agents_mcp")
    monkeypatch.setattr(telemetry, "configure_telemetry", lambda _service: True)
    monkeypatch.setattr(telemetry, "_content_capture_enabled", True)
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    yield exporter
    provider.shutdown()


def test_focused_scope_skips_transport_without_inserting_hidden_parents(focused_traces):
    from flight_agent.event_delivery import consume_event_trace, publish_durable_event
    from types import SimpleNamespace

    published = {}

    class Broker:
        async def publish(self, _subject, _payload, **kwargs):
            published.update(kwargs)

    with telemetry.trace_operation("agent.monitor.detect_disruption", service_name="monitor") as parent:
        parent_context = parent.get_span_context()
        headers = telemetry.trace_headers()
        with telemetry.trace_operation("POST /mcp", service_name="monitor") as hidden:
            assert hidden is None
            assert telemetry.trace_headers() == headers
        asyncio.run(publish_durable_event(Broker(), {
            "event_id": "synthetic-event", "event_type": "disruption_candidate",
            "occurred_at": "2026-09-15T06:00:00Z", "payload": {"delay_minutes": 45},
            "subject": "travel.disruption_candidate.v1", "trace_headers": headers,
        }))
    assert published["headers"]["traceparent"] == headers["traceparent"]
    message = SimpleNamespace(headers=published["headers"])
    with consume_event_trace(message, service_name="eval", operation="messaging.consume.disruption_candidate"):
        with telemetry.trace_operation("agent.eval.apply_policy", service_name="eval"):
            with telemetry.trace_operation("mcp.search_flights", service_name="search", kind="tool"):
                pass
    spans = {span.name: span for span in focused_traces.get_finished_spans()}
    assert set(spans) == {"agent.monitor.detect_disruption", "agent.eval.apply_policy", "mcp.search_flights"}
    assert spans["agent.eval.apply_policy"].parent.span_id == parent_context.span_id
    assert spans["mcp.search_flights"].parent.span_id == spans["agent.eval.apply_policy"].context.span_id
    assert all(span.context.trace_id == parent_context.trace_id for span in spans.values())


def test_focused_scope_suppresses_http_even_when_http_mode_is_all(focused_traces, monkeypatch):
    monkeypatch.setenv("OTEL_HTTP_TRACE_MODE", "all")
    app = FastAPI()
    telemetry.install_trace_middleware(app, service_name="travel-api")

    @app.get("/poll")
    async def poll():
        return {"status": "ok"}

    @app.post("/v1/trips/activate")
    async def activate():
        telemetry.set_current_span_content(input_value={"task": "activate synthetic trip"}, output_value={"status": "active"})
        return {"status": "active"}

    client = TestClient(app)
    assert client.get("/poll").status_code == 200
    assert client.post("/v1/trips/activate").status_code == 200
    spans = focused_traces.get_finished_spans()
    assert [span.name for span in spans] == ["agent.orchestrator.trip_pipeline"]
    assert spans[0].attributes["travel.trace.has_input"] is True
    assert spans[0].attributes["travel.trace.has_output"] is True
    assert not any(key.startswith("http.") for key in spans[0].attributes)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_focused_errors_have_safe_outcomes_without_raw_exception_data(focused_traces, asynchronous):
    def fail():
        raise RuntimeError("private-key-and-phone-must-not-be-exported")

    async def async_fail():
        fail()

    operation = telemetry.traced(
        "mcp.synthetic_error", service_name="test", kind="tool",
        content_input=lambda: {"flight_iata": "NB204"},
    )(async_fail if asynchronous else fail)
    with pytest.raises(RuntimeError):
        asyncio.run(operation()) if asynchronous else operation()
    span, = focused_traces.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.attributes["travel.trace.has_input"] is True
    assert span.attributes["travel.trace.has_output"] is True
    assert json.loads(span.attributes["gen_ai.completion.0.content"]) == {"status": "error", "error_type": "RuntimeError"}
    assert "private-key" not in str(span.attributes)
    assert not span.events


def test_explicit_safe_failure_output_is_not_replaced_by_generic_exception(focused_traces):
    with pytest.raises(RuntimeError):
        with telemetry.trace_operation("mcp.send_notification", service_name="test"):
            telemetry.set_current_span_content(
                input_value={"channel": "sms"},
                output_value={"status": "failed", "error_code": "TWILIO_HTTP_400"},
            )
            raise RuntimeError("unexported detail")
    span, = focused_traces.get_finished_spans()
    assert json.loads(span.attributes["gen_ai.completion.0.content"])["error_code"] == "TWILIO_HTTP_400"
    assert span.status.status_code == trace.StatusCode.ERROR
