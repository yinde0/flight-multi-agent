from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_langsmith_services_disable_internal_instrumentation_before_startup():
    config = yaml.safe_load((ROOT / "compose.langsmith.yaml").read_text())
    for name, service in config["services"].items():
        if name == "otel-collector":
            continue
        environment = service["environment"]
        assert environment["OTEL_TRACE_SCOPE"] == "agents_mcp", name
        assert environment["OTEL_INSTRUMENTATION_A2A_SDK_ENABLED"] == "false", name
        assert environment.get("CREWAI_OTEL_ENABLED", "false") == "false", name


def test_langsmith_collector_requires_name_and_both_content_markers():
    config = yaml.safe_load((ROOT / "otel-collector-langsmith.yaml").read_text())
    filters = config["processors"]["filter/agent_mcp_io"]
    assert filters["error_mode"] == "propagate"
    assert filters["traces"]["span"][:3] == [
        'not IsMatch(name, "^(agent|mcp)[.]")',
        'attributes["travel.trace.has_input"] != true',
        'attributes["travel.trace.has_output"] != true',
    ]
    assert config["service"]["pipelines"]["traces"]["processors"] == [
        "filter/agent_mcp_io", "transform/business_metadata", "batch",
    ]


def test_a2a_instrumentation_switch_preserves_the_visible_agent_parent():
    # A2A reads its switch at import time, so test it in a clean process exactly
    # as Compose supplies it rather than reloading global SDK state in pytest.
    script = '''
from a2a.utils.telemetry import trace_function, otel_enabled
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
assert not otel_enabled
provider = TracerProvider()
exporter = InMemorySpanExporter()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("application")
@trace_function
def internal_a2a_handler():
    with tracer.start_as_current_span("agent.document.parse_itinerary"):
        return "parsed"
with tracer.start_as_current_span("agent.orchestrator.trip_pipeline") as parent:
    parent_id = parent.get_span_context().span_id
    assert internal_a2a_handler() == "parsed"
spans = exporter.get_finished_spans()
assert [span.name for span in spans] == ["agent.document.parse_itinerary", "agent.orchestrator.trip_pipeline"]
assert spans[0].parent.span_id == parent_id
provider.shutdown()
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "OTEL_INSTRUMENTATION_A2A_SDK_ENABLED": "false"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
