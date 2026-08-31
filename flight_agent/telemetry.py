from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import threading

from collections import Counter
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse


_lock = threading.Lock()
_configured = False
_export_enabled = False
_crewai_instrumented = False
_content_capture_enabled = False
_service_name = "travel-service"
_operation_counts: Counter[tuple[str, str]] = Counter()
TRACE_CONTEXT_HEADERS = ("traceparent", "tracestate")
HTTP_TRACE_EXCLUDED_PATHS = frozenset(
    {
        "/health/live",
        "/metrics",
        "/v1/observability/status",
    }
)
HTTP_TRACE_MODE_ENV = "OTEL_HTTP_TRACE_MODE"
AGENT_MCP_TRACE_PREFIXES = ("agent.", "mcp.")
HTTP_AGENT_ROOTS = {
    (
        "travel-api",
        "POST",
        "/v1/trips/activate",
    ): "agent.orchestrator.trip_pipeline",
}


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


def agent_mcp_trace_scope() -> bool:
    return os.getenv("OTEL_TRACE_SCOPE", "all").strip().lower() == "agents_mcp"


def has_trace_content(value: Any) -> bool:
    """Reject empty containers/strings, but retain real zero/false outcomes."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(has_trace_content(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_trace_content(item) for item in value)
    return True


def hash_reference(value: Any) -> str:
    """Return a stable correlation value without exporting the source PII."""

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def development_content_capture_enabled() -> bool:
    """Allow content only behind an explicit switch in development."""

    return (
        os.getenv("DEPLOYMENT_ENVIRONMENT", "development").strip().lower()
        == "development"
        and _enabled("OTEL_TRACE_CONTENT_ENABLED")
    )


def trace_headers(carrier: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Inject the current W3C trace context into a small, privacy-safe carrier."""

    headers = {
        str(key).lower(): str(value)
        for key, value in (carrier or {}).items()
        if str(key).lower() in TRACE_CONTEXT_HEADERS and value is not None
    }
    try:
        from opentelemetry.propagate import inject

        injected: dict[str, str] = {}
        inject(injected)
        for key in TRACE_CONTEXT_HEADERS:
            if injected.get(key):
                headers[key] = str(injected[key])
    except Exception:
        pass
    return headers


@contextmanager
def extracted_trace_context(
    carrier: Mapping[str, Any] | None,
) -> Iterator[None]:
    """Attach an incoming W3C context and always restore the previous context."""

    token = None
    try:
        from opentelemetry.context import attach
        from opentelemetry.propagate import extract

        normalized = {
            str(key).lower(): str(value)
            for key, value in (carrier or {}).items()
            if str(key).lower() in TRACE_CONTEXT_HEADERS and value is not None
        }
        token = attach(extract(normalized))
    except Exception:
        token = None
    try:
        yield
    finally:
        if token is not None:
            try:
                from opentelemetry.context import detach

                detach(token)
            except Exception:
                pass


def current_trace_id() -> str | None:
    """Return the active trace ID for correlation, never a source-data value."""

    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            return format(context.trace_id, "032x")
    except Exception:
        pass
    return None


def _content_text(value: Any) -> str:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except Exception:
        rendered = str(value)
    maximum = max(
        256, int(os.getenv("OTEL_TRACE_CONTENT_MAX_CHARS", "12000"))
    )
    if len(rendered) <= maximum:
        return rendered
    return rendered[:maximum] + "...[truncated]"


def _set_span_content(span: Any, direction: str, value: Any) -> None:
    if span is None or not _content_capture_enabled or value is None:
        return
    try:
        if not has_trace_content(value):
            return
        if direction == "input":
            span.set_attribute("gen_ai.prompt.0.role", "user")
            span.set_attribute("gen_ai.prompt.0.content", _content_text(value))
        else:
            span.set_attribute("gen_ai.completion.0.role", "assistant")
            span.set_attribute(
                "gen_ai.completion.0.content", _content_text(value)
            )
        # The LangSmith collector requires both markers and both content fields.
        # A name, a role, or an empty JSON object alone is not a useful run.
        span.set_attribute(f"travel.trace.has_{direction}", True)
        span.set_attribute(
            "langsmith.metadata.content_capture", "development_explicit"
        )
    except Exception:
        # Observability must remain fail-open for business processing.
        return


def set_current_span_content(
    *, input_value: Any = None, output_value: Any = None
) -> None:
    """Attach a safe business view to the active application span."""

    try:
        from opentelemetry import trace

        span = trace.get_current_span()
    except Exception:
        return
    _set_span_content(span, "input", input_value)
    _set_span_content(span, "output", output_value)


def set_current_span_attributes(attributes: Mapping[str, Any]) -> None:
    """Attach low-cardinality business attributes to the active span."""

    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        for key, value in attributes.items():
            if value is not None and isinstance(value, (str, bool, int, float)):
                span.set_attribute(key, value)
    except Exception:
        return


def _http_trace_operation(
    *, service_name: str, method: str, path: str
) -> tuple[str, bool] | None:
    """Resolve whether an HTTP request should be a visible LangSmith run.

    ``all`` preserves the generic OTEL behavior used by the local observability
    stack. ``agent_roots`` keeps only selected public workflow entry points,
    while ``off`` retains context propagation without creating transport runs.
    """

    mode = os.getenv(HTTP_TRACE_MODE_ENV, "all").strip().lower()
    if mode == "off":
        return None
    if mode == "agent_roots" or agent_mcp_trace_scope():
        operation = HTTP_AGENT_ROOTS.get((service_name, method.upper(), path))
        return (operation, True) if operation else None
    return ("http.server", False)


def configure_telemetry(service_name: str) -> bool:
    """Configure non-blocking OTLP export once per process.

    Failure to configure or export telemetry must never fail travel processing.
    """

    global _configured, _export_enabled, _crewai_instrumented
    global _content_capture_enabled, _service_name
    with _lock:
        _service_name = service_name
        if _configured:
            return _export_enabled
        _configured = True
        if not _enabled("OTEL_TRACING_ENABLED"):
            return False
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
        if not endpoint:
            return False
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = trace.get_tracer_provider()
            if not isinstance(provider, TracerProvider):
                provider = TracerProvider(
                    resource=Resource.create(
                        {
                            "service.name": service_name,
                            "service.version": "0.1.0",
                            "deployment.environment": os.getenv(
                                "DEPLOYMENT_ENVIRONMENT", "development"
                            ),
                        }
                    )
                )
                trace.set_tracer_provider(provider)
            exporter = OTLPSpanExporter(
                endpoint=endpoint,
                timeout=float(os.getenv("OTEL_EXPORT_TIMEOUT_SECONDS", "2")),
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            _export_enabled = True
            _content_capture_enabled = development_content_capture_enabled()

            # Manual agent spans already include the safe prompt/result views.
            # Auto-instrumentation introduces hidden intermediate parents and
            # duplicate internal CrewAI spans in an agent-only trace tree.
            if _enabled("CREWAI_OTEL_ENABLED") and not agent_mcp_trace_scope():
                os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
                os.environ.setdefault("CREWAI_DISABLE_TRACKING", "true")
                os.environ.setdefault("CREWAI_DISABLE_VERSION_CHECK", "true")
                # Production always resolves to false, even if a container is
                # accidentally launched with a development content flag.
                os.environ["TRACELOOP_TRACE_CONTENT"] = (
                    "true" if _content_capture_enabled else "false"
                )
                from opentelemetry.instrumentation.crewai import CrewAIInstrumentor

                CrewAIInstrumentor().instrument(tracer_provider=provider)
                _crewai_instrumented = True
        except Exception:
            _export_enabled = False
            _content_capture_enabled = False
        return _export_enabled


def _record(operation: str, outcome: str) -> None:
    with _lock:
        _operation_counts[(operation, outcome)] += 1


@contextmanager
def trace_operation(
    operation: str,
    *,
    service_name: str,
    kind: str = "chain",
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    enabled = configure_telemetry(service_name)
    span_context = None
    focused = agent_mcp_trace_scope()
    if enabled and (not focused or operation.startswith(AGENT_MCP_TRACE_PREFIXES)):
        try:
            from opentelemetry import trace

            tracer = trace.get_tracer(service_name)
            span_context = tracer.start_as_current_span(
                operation,
                record_exception=not focused,
                set_status_on_exception=not focused,
            )
        except Exception:
            span_context = None
    try:
        if span_context is None:
            yield None
        else:
            with span_context as span:
                span.set_attribute("langsmith.span.kind", kind)
                span.set_attribute("langsmith.metadata.service", service_name)
                for key, value in (attributes or {}).items():
                    if value is not None and isinstance(
                        value, (str, bool, int, float)
                    ):
                        span.set_attribute(key, value)
                try:
                    yield span
                except Exception as error:
                    if focused:
                        from opentelemetry.trace import Status, StatusCode

                        # An error is an outcome too. Do not export raw exception
                        # messages/stack traces, which can contain provider URLs,
                        # credentials, or traveler data.
                        if not (span.attributes or {}).get("travel.trace.has_output"):
                            _set_span_content(
                                span,
                                "output",
                                {"status": "error", "error_type": type(error).__name__},
                            )
                        span.set_status(Status(StatusCode.ERROR))
                    raise
    except Exception:
        _record(operation, "error")
        raise


def traced(
    operation: str,
    *,
    service_name: str,
    kind: str = "chain",
    attributes: Callable[..., dict[str, Any]] | None = None,
    result_outcome: Callable[[Any], str] | None = None,
    content_input: Callable[..., Any] | None = None,
    content_output: Callable[[Any], Any] | None = None,
):
    """Trace a sync/async boundary and expose low-cardinality operation counts."""

    def decorator(function):
        def resolved_attributes(args, kwargs) -> dict[str, Any]:
            if attributes is None:
                return {}
            try:
                return attributes(*args, **kwargs)
            except Exception:
                return {}

        def resolved_outcome(result: Any) -> str:
            if result_outcome is None:
                return "success"
            try:
                return str(result_outcome(result))
            except Exception:
                return "unknown"

        def resolved_input_content(args, kwargs) -> Any:
            if not _content_capture_enabled or content_input is None:
                return None
            try:
                return content_input(*args, **kwargs)
            except Exception:
                return None

        def resolved_output_content(result: Any) -> Any:
            if not _content_capture_enabled or content_output is None:
                return None
            try:
                return content_output(result)
            except Exception:
                return None

        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def async_wrapper(*args, **kwargs):
                with trace_operation(
                    operation,
                    service_name=service_name,
                    kind=kind,
                    attributes=resolved_attributes(args, kwargs),
                ) as span:
                    _set_span_content(
                        span, "input", resolved_input_content(args, kwargs)
                    )
                    result = await function(*args, **kwargs)
                    _set_span_content(
                        span, "output", resolved_output_content(result)
                    )
                _record(operation, resolved_outcome(result))
                return result

            return async_wrapper

        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            with trace_operation(
                operation,
                service_name=service_name,
                kind=kind,
                attributes=resolved_attributes(args, kwargs),
            ) as span:
                _set_span_content(
                    span, "input", resolved_input_content(args, kwargs)
                )
                result = function(*args, **kwargs)
                _set_span_content(
                    span, "output", resolved_output_content(result)
                )
            _record(operation, resolved_outcome(result))
            return result

        return wrapper

    return decorator


def metrics_text() -> str:
    with _lock:
        counts = dict(_operation_counts)
        export_enabled = _export_enabled
        crewai_enabled = _crewai_instrumented
        content_enabled = _content_capture_enabled
        service = _service_name
    lines = [
        "# HELP travel_operation_executions_total Completed application operations.",
        "# TYPE travel_operation_executions_total counter",
    ]
    for (operation, outcome), value in sorted(counts.items()):
        lines.append(
            "travel_operation_executions_total"
            f'{{operation="{operation}",outcome="{outcome}"}} {value}'
        )
    lines.extend(
        [
            "# HELP travel_trace_export_enabled Whether OTLP trace export is enabled.",
            "# TYPE travel_trace_export_enabled gauge",
            "travel_trace_export_enabled " + ("1" if export_enabled else "0"),
            "# HELP travel_crewai_instrumentation_enabled Whether privacy-safe CrewAI instrumentation is enabled.",
            "# TYPE travel_crewai_instrumentation_enabled gauge",
            "travel_crewai_instrumentation_enabled "
            + ("1" if crewai_enabled else "0"),
            "# HELP travel_trace_content_capture_enabled Whether explicit development content capture is enabled.",
            "# TYPE travel_trace_content_capture_enabled gauge",
            "travel_trace_content_capture_enabled "
            + ("1" if content_enabled else "0"),
            f'# travel_service_name "{service}"',
        ]
    )
    return "\n".join(lines) + "\n"


def install_telemetry_routes(
    app: FastAPI, *, service_name: str, include_metrics: bool = True
) -> None:
    configure_telemetry(service_name)
    install_trace_middleware(app, service_name=service_name)

    if include_metrics:

        @app.get("/metrics", include_in_schema=False)
        async def telemetry_metrics() -> PlainTextResponse:
            return PlainTextResponse(
                metrics_text(), media_type="text/plain; version=0.0.4"
            )

    @app.get("/v1/observability/status", tags=["observability"])
    async def telemetry_status() -> dict[str, Any]:
        return {
            "service": service_name,
            "trace_export_enabled": _export_enabled,
            "crewai_instrumentation_enabled": _crewai_instrumented,
            "content_capture_enabled": _content_capture_enabled,
            "trace_scope": "agents_mcp" if agent_mcp_trace_scope() else "all",
            "content_capture_scope": (
                "development_explicit"
                if _content_capture_enabled
                else "disabled"
            ),
        }


def install_trace_middleware(app: Any, *, service_name: str) -> None:
    """Continue inbound W3C context and expose the trace ID on HTTP responses."""

    if getattr(app.state, "travel_trace_middleware_installed", False):
        return
    app.state.travel_trace_middleware_installed = True
    configure_telemetry(service_name)

    async def distributed_trace_middleware(request, call_next):
        with extracted_trace_context(request.headers):
            operation = (
                None
                if request.url.path in HTTP_TRACE_EXCLUDED_PATHS
                else _http_trace_operation(
                    service_name=service_name,
                    method=request.method,
                    path=request.url.path,
                )
            )
            if operation is None:
                response = await call_next(request)
                active_trace_id = current_trace_id()
                if active_trace_id:
                    response.headers["X-Trace-Id"] = active_trace_id
                return response
            operation_name, agent_root = operation
            with trace_operation(
                operation_name,
                service_name=service_name,
                kind="chain",
                attributes={} if agent_root else {"http.request.method": request.method},
            ) as span:
                response = await call_next(request)
                route = request.scope.get("route")
                route_path = getattr(route, "path", None)
                if span is not None and not agent_root:
                    resolved_route = str(route_path or request.url.path)
                    try:
                        span.update_name(f"{request.method} {resolved_route}")
                    except Exception:
                        pass
                    span.set_attribute("http.response.status_code", response.status_code)
                    if route_path:
                        span.set_attribute("http.route", str(route_path))
                    _set_span_content(
                        span,
                        "input",
                        {"method": request.method, "route": resolved_route},
                    )
                    _set_span_content(
                        span, "output", {"status_code": response.status_code}
                    )
                active_trace_id = current_trace_id()
                if active_trace_id:
                    response.headers["X-Trace-Id"] = active_trace_id
                return response

    # FastAPI exposes ``@app.middleware`` but MCP's Streamable HTTP transport
    # returns a plain Starlette app. BaseHTTPMiddleware is supported by both.
    from starlette.middleware.base import BaseHTTPMiddleware

    app.add_middleware(BaseHTTPMiddleware, dispatch=distributed_trace_middleware)
