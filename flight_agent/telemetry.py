from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import threading

from collections import Counter
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse


_lock = threading.Lock()
_configured = False
_export_enabled = False
_crewai_instrumented = False
_content_capture_enabled = False
_service_name = "travel-service"
_operation_counts: Counter[tuple[str, str]] = Counter()


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


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
        if direction == "input":
            span.set_attribute("gen_ai.prompt.0.role", "user")
            span.set_attribute("gen_ai.prompt.0.content", _content_text(value))
        else:
            span.set_attribute("gen_ai.completion.0.role", "assistant")
            span.set_attribute(
                "gen_ai.completion.0.content", _content_text(value)
            )
        span.set_attribute(
            "langsmith.metadata.content_capture", "development_explicit"
        )
    except Exception:
        # Observability must remain fail-open for business processing.
        return


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

            if (
                service_name == "document-agent"
                and _enabled("CREWAI_OTEL_ENABLED")
            ):
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
    if enabled:
        try:
            from opentelemetry import trace

            tracer = trace.get_tracer(service_name)
            span_context = tracer.start_as_current_span(operation)
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
                yield span
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
            "content_capture_scope": (
                "development_explicit"
                if _content_capture_enabled
                else "disabled"
            ),
        }
