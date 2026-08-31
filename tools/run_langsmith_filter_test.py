"""Verify the deployed LangSmith filter without activating trips or sending SMS."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx


def emit_probe(*, agent_preview: bool = False) -> dict:
    from opentelemetry import trace

    from flight_agent import telemetry
    from flight_agent.weather_mcp_client import StreamableHttpWeatherMcpClient

    if not telemetry.agent_mcp_trace_scope():
        raise RuntimeError("Agent/MCP tracing scope is not enabled in this container")
    if not telemetry.configure_telemetry("monitor-agent") or not telemetry.development_content_capture_enabled():
        raise RuntimeError("Explicit development tracing must be enabled")
    root_name = f"agent.observability.filter_check.{uuid.uuid4().hex[:12]}"
    expected = [root_name, "mcp.get_airport_weather"]
    dropped = []
    with telemetry.trace_operation(root_name, service_name="monitor-agent") as root:
        telemetry._set_span_content(root, "input", {"task": "Synthetic trace-filter verification; no traveler actions."})
        tracer = trace.get_tracer("synthetic-filter-probe")
        cases = [
            ("http.server", {"method": "GET"}, {"status_code": 200}),
            ("POST http://synthetic-service:9999/mcp", {"request": "synthetic"}, {"status": "ok"}),
            ("messaging.publish", {"event": "synthetic"}, {"accepted": True}),
            ("CrewAI.Task.execute", {"task": "synthetic"}, {"status": "ok"}),
            ("agent.synthetic.missing_input", None, {"status": "ok"}),
            ("mcp.synthetic.missing_output", {"task": "synthetic"}, None),
            ("agent.synthetic.empty_input", {"empty": []}, {"status": "ok"}),
            ("mcp.synthetic.empty_output", {"task": "synthetic"}, "  "),
        ]
        for name, input_value, output_value in cases:
            # Deliberately bypass source-side suppression to test the collector.
            with tracer.start_as_current_span(name) as span:
                telemetry._set_span_content(span, "input", input_value)
                telemetry._set_span_content(span, "output", output_value)
            dropped.append(name)
        # Simulate an external instrumentor's empty-but-marked payload as well.
        with tracer.start_as_current_span("agent.synthetic.blank_marked_output") as span:
            telemetry._set_span_content(span, "input", {"task": "synthetic"})
            span.set_attribute("travel.trace.has_output", True)
            span.set_attribute("gen_ai.completion.0.content", "  ")
        dropped.append("agent.synthetic.blank_marked_output")

        if agent_preview:
            from flight_agent.communication_a2a_client import A2ACommunicationAgentClient
            from flight_agent.disruption_explanation import DisruptionExplanationRequest

            # Explanation only: this contract carries no notification authority.
            A2ACommunicationAgentClient("http://communication-agent:8017").explain(
                DisruptionExplanationRequest(
                    category="DELAY", verdict="NOTIFY",
                    reason_codes=["DELAY_NOTIFY_THRESHOLD"], delay_minutes=45,
                    search_requested=False,
                )
            )
            expected.append("agent.communication.explain_disruption")

        # A real read-only MCP call exercises the new safe tool input/output view.
        weather = StreamableHttpWeatherMcpClient("http://weather-mcp:8006/mcp").get_airport_weather(
            airport="LHR", target_at="2026-09-15T08:20:00Z", replay_key=root_name,
        )
        telemetry._set_span_content(root, "output", {
            "airport": weather.airport, "weather_risk": weather.risk_level,
            "synthetic_filter_cases_submitted": len(dropped), "sms_sent": False,
        })
        root.set_attribute("http.route", "/synthetic-check")
        root.set_attribute("server.port", 9999)
        root.set_attribute("url.full", "http://synthetic-service:9999/synthetic-check")
    if not trace.get_tracer_provider().force_flush(timeout_millis=10000):
        raise RuntimeError("Trace export did not flush before the timeout")
    return {"root_name": root_name, "expected_names": expected, "dropped_names": dropped}


def run_records(payload) -> list[dict]:
    if isinstance(payload, list):
        return [run for value in payload for run in run_records(value)]
    if isinstance(payload, dict):
        if isinstance(payload.get("name"), str) and payload.get("id"):
            return [payload]
        return [run for value in payload.values() for run in run_records(value)]
    return []


def verify_probe(probe: dict, config: dict) -> dict:
    endpoint = str(config.get("LANGSMITH_ENDPOINT") or "").strip().rstrip("/")
    key = str(config.get("LANGSMITH_API_KEY") or "").strip()
    project = str(config.get("LANGSMITH_PROJECT") or "").strip()
    if not endpoint or not key or not project:
        raise RuntimeError("Required LangSmith configuration is missing")
    api_root = endpoint if endpoint.endswith("/api/v1") else endpoint + "/api/v1"
    select = ["id", "name", "trace_id", "parent_run_id", "inputs", "outputs", "extra"]
    matching = []
    with httpx.Client(timeout=20, trust_env=False, headers={"X-API-Key": key}) as client:
        def query(body):
            for attempt in range(4):
                result = client.post(api_root + "/runs/query", json=body)
                if result.status_code not in {429, 502, 503} or attempt == 3:
                    result.raise_for_status()
                    return result
                try:
                    delay = float(result.headers.get("retry-after", 5 * (attempt + 1)))
                except ValueError:
                    delay = 10
                time.sleep(min(30, max(5, delay)))
            raise RuntimeError("LangSmith query remained unavailable")

        response = client.get(api_root + "/sessions", params={"name": project})
        response.raise_for_status()
        sessions = response.json()
        project_id = sessions[0]["id"] if isinstance(sessions, list) and sessions else None
        if not project_id:
            raise RuntimeError("Configured LangSmith project was not found")
        for _ in range(15):
            response = query({
                "session": [project_id], "filter": f'eq(name, "{probe["root_name"]}")',
                "select": select, "limit": 10,
            })
            response.raise_for_status()
            roots = run_records(response.json())
            if roots:
                response = query({
                    "session": [project_id], "trace": roots[0]["trace_id"],
                    "select": select, "limit": 100,
                })
                response.raise_for_status()
                matching = run_records(response.json())
                if len(matching) >= len(probe["expected_names"]):
                    break
            time.sleep(5)
    names = {run["name"] for run in matching}
    io_visible = bool(matching) and all(run.get("inputs") and run.get("outputs") for run in matching)
    known_ids = {run["id"] for run in matching}
    parents_valid = bool(matching) and all(not run.get("parent_run_id") or run["parent_run_id"] in known_ids for run in matching)
    metadata = json.dumps([run.get("extra") for run in matching])
    transport_metadata_absent = all(value not in metadata for value in ("synthetic-check", "synthetic-service", "server.port", "http.route", "url.full"))
    return {
        "passed": names == set(probe["expected_names"]) and io_visible and parents_valid and transport_metadata_absent,
        "visible_runs": sorted(names),
        "all_inputs_and_outputs_visible": bool(io_visible),
        "parent_links_preserved": parents_valid,
        "transport_metadata_absent": transport_metadata_absent,
        "unwanted_runs_found": sorted(names.intersection(probe["dropped_names"])),
        "synthetic_drop_cases": len(probe["dropped_names"]),
        "sms_sent": False,
        "trace_content_printed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--container", default="multi_agent-monitor-agent-1")
    parser.add_argument("--agent-preview", action="store_true", help="Also call the real Communication Agent over A2A (may use the configured LLM; never sends SMS)")
    args = parser.parse_args()
    if args.emit:
        print(json.dumps(emit_probe(agent_preview=args.agent_preview)))
        return 0
    from dotenv import dotenv_values

    root = Path(__file__).resolve().parents[1]
    # Pass only checked-in test code, never host credentials, into the container.
    completed = subprocess.run(
        ["docker", "exec", "-i", args.container, "python", "-", "--emit"] + (["--agent-preview"] if args.agent_preview else []),
        input=Path(__file__).read_text(encoding="utf-8"), text=True,
        capture_output=True, timeout=60, check=False,
    )
    if completed.returncode:
        raise RuntimeError("Synthetic probe failed; check container readiness and development tracing settings")
    probe = json.loads(completed.stdout.strip().splitlines()[-1])
    print("Verifying synthetic LangSmith trace: " + probe["root_name"], flush=True)
    report = verify_probe(probe, dotenv_values(root / ".env"))
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
