from __future__ import annotations

import argparse
import json
import time
import uuid

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SPANS = {
    "agent.orchestrator.trip_pipeline",
    "agent.document.parse_itinerary",
    "agent.orchestrator.monitor_leg",
    "agent.monitor.detect_disruption",
    "mcp.get_flight_status",
    "mcp.get_airport_weather",
    "messaging.publish",
    "messaging.consume.disruption_candidate",
    "agent.eval.apply_policy",
    "agent.eval.review_with_crewai",
    "messaging.consume.disruption_confirmed",
    "agent.orchestrator.notify_traveler",
    "agent.communication.explain_disruption",
    "mcp.send_notification",
    "agent.orchestrator.search_rebooking",
    "mcp.search_flights",
}
AGENT_SPANS = {
    name for name in EXPECTED_SPANS if name.startswith("agent.")
}
TRACE_ANCHORS = {
    "agent.document.parse_itinerary",
    "agent.orchestrator.monitor_leg",
    "agent.monitor.detect_disruption",
    "agent.eval.apply_policy",
    "agent.orchestrator.notify_traveler",
    "agent.communication.explain_disruption",
    "agent.orchestrator.search_rebooking",
}
TRACE_SELECT = [
    "id",
    "trace_id",
    "parent_run_id",
    "name",
    "run_type",
    "start_time",
    "inputs",
    "outputs",
    "session_id",
]


def values(value: object) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if isinstance(value.get("name"), str):
            found.append(value)
        for child in value.values():
            found.extend(values(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(values(child))
    return found


def normalized_trace_id(value: object) -> str:
    return str(value or "").replace("-", "").lower()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replay one synthetic activation-to-action workflow and verify that "
            "its HTTP, A2A, scheduled poll, NATS, Eval, and MCP spans share one "
            "unambiguous LangSmith trace."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()

    config = dotenv_values(ROOT / ".env")
    api_key = str(config.get("LANGSMITH_API_KEY") or "").strip()
    project = str(config.get("LANGSMITH_PROJECT") or "").strip()
    endpoint = str(config.get("LANGSMITH_ENDPOINT") or "").strip().rstrip("/")
    if not api_key or not project or not endpoint:
        raise RuntimeError("Required LangSmith configuration is missing")

    pdf = ROOT / "output" / "pdf" / "synthetic_direct_eticket.pdf"
    run_id = uuid.uuid4().hex[:12]
    trip_id = f"trip-v10-{run_id}"
    started_at = datetime.now(timezone.utc)
    base_url = args.base_url.rstrip("/")

    with httpx.Client(timeout=120, trust_env=False) as client:
        activation = client.post(
            f"{base_url}/v1/trips/activate",
            files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={
                "trip_id": trip_id,
                "traveler_ref": "traveler-synthetic-trace",
                "fixture_id": f"doc-v10-{run_id}",
            },
        )
        activation.raise_for_status()
        trace_id = normalized_trace_id(activation.headers.get("x-trace-id"))

        ticks: list[dict[str, Any]] = []
        for now in (
            "2026-09-15T06:00:00Z",
            "2026-09-15T06:00:00Z",
            "2026-09-15T06:10:00Z",
            "2026-09-15T06:10:00Z",
        ):
            response = client.post(
                f"{base_url}/v1/orchestration/tick",
                json={"now": now, "maximum_legs": 10},
            )
            response.raise_for_status()
            ticks.append(response.json())

        consequences = [
            result
            for tick in ticks
            for result in tick.get("results", [])
            if result.get("verdict") == "NOTIFY_AND_SEARCH"
        ]

        api_root = endpoint if endpoint.endswith("/api/v1") else f"{endpoint}/api/v1"
        headers = {"X-API-Key": api_key}
        project_response = client.get(
            f"{api_root}/sessions", headers=headers, params={"name": project}
        )
        project_response.raise_for_status()
        projects = project_response.json()
        project_record = (
            projects[0] if isinstance(projects, list) and projects else projects
        )
        project_id = str(project_record.get("id") or "")
        if not project_id:
            raise RuntimeError("Configured LangSmith project was not found")
        if len(trace_id) != 32:
            raise RuntimeError("Travel API did not return a valid W3C trace ID")
        langsmith_trace_id = str(uuid.UUID(hex=trace_id))

        matching_runs: list[dict[str, Any]] = []
        diagnostic_runs: list[dict[str, Any]] = []
        diagnostic_groups: dict[str, set[str]] = {}
        selected_trace_id = trace_id
        trace_selection = "returned_w3c_id"
        query_status = 0
        response = client.post(
            f"{api_root}/runs/query",
            headers=headers,
            json={"trace": langsmith_trace_id, "select": TRACE_SELECT},
        )
        query_status = response.status_code
        if query_status not in {429, 502, 503}:
            response.raise_for_status()
            trace_runs = [
                run
                for run in values(response.json())
                if normalized_trace_id(run.get("trace_id")) == trace_id
            ]
            matching_runs = [
                run
                for run in trace_runs
                if str(run.get("session_id") or "") == project_id
            ]

        if not matching_runs:
            diagnostic_names = EXPECTED_SPANS
            name_filter = "or(" + ",".join(
                f'eq(name, "{name}")' for name in sorted(diagnostic_names)
            ) + ")"
            for _ in range(12):
                diagnostic_response = client.post(
                    f"{api_root}/runs/query",
                    headers=headers,
                    json={
                        "session": [project_id],
                        "start_time": (
                            started_at - timedelta(seconds=5)
                        ).isoformat(),
                        "filter": name_filter,
                        "select": ["id", "name", "trace_id", "session_id"],
                        "limit": 100,
                    },
                )
                query_status = diagnostic_response.status_code
                if query_status in {429, 502, 503}:
                    time.sleep(3)
                    continue
                diagnostic_response.raise_for_status()
                diagnostic_runs = values(diagnostic_response.json())
                diagnostic_groups = {}
                for run in diagnostic_runs:
                    name = str(run.get("name") or "")
                    candidate_trace_id = normalized_trace_id(run.get("trace_id"))
                    if name in EXPECTED_SPANS and candidate_trace_id:
                        diagnostic_groups.setdefault(
                            candidate_trace_id, set()
                        ).add(name)
                anchored_groups = [
                    candidate_trace_id
                    for candidate_trace_id, group in diagnostic_groups.items()
                    if TRACE_ANCHORS <= group
                ]
                if len(anchored_groups) == 1:
                    selected_trace_id = anchored_groups[0]
                    selected_response = client.post(
                        f"{api_root}/runs/query",
                        headers=headers,
                        json={
                            "trace": str(uuid.UUID(hex=selected_trace_id)),
                            "select": TRACE_SELECT,
                        },
                    )
                    query_status = selected_response.status_code
                    if query_status == 200:
                        matching_runs = [
                            run
                            for run in values(selected_response.json())
                            if normalized_trace_id(run.get("trace_id"))
                            == selected_trace_id
                            and str(run.get("session_id") or "") == project_id
                        ]
                        trace_selection = "current_window_anchor_group"
                        names = {str(run.get("name")) for run in matching_runs}
                        if EXPECTED_SPANS <= names:
                            break
                time.sleep(3)

    names = {str(run.get("name")) for run in matching_runs}
    missing = sorted(EXPECTED_SPANS - names)
    advisory_runs = [
        run
        for run in matching_runs
        if run.get("name") == "agent.eval.review_with_crewai"
    ]
    advisory_content_visible = any(
        bool(run.get("inputs")) and bool(run.get("outputs"))
        for run in advisory_runs
    )
    agent_runs = [
        run for run in matching_runs if run.get("name") in AGENT_SPANS
    ]
    agent_io_visible = {
        str(run.get("name")): bool(run.get("inputs")) and bool(run.get("outputs"))
        for run in agent_runs
    }
    all_agent_io_visible = AGENT_SPANS <= {
        name for name, visible in agent_io_visible.items() if visible
    }
    transport_runs = sorted(
        {
            str(run.get("name"))
            for run in matching_runs
            if str(run.get("name")) == "http.server"
            or str(run.get("name")).startswith(
                ("GET ", "POST ", "PUT ", "PATCH ", "DELETE ")
            )
        }
    )
    one_consequence = len(consequences) == 1
    action_complete = bool(
        one_consequence
        and consequences[0].get("notification_status") == "delivered"
        and consequences[0].get("search_status") == "completed"
        and consequences[0].get("notification_id")
        and consequences[0].get("search_id")
    )
    duplicate_ticks_suppressed = [tick.get("claimed_count") for tick in ticks] == [
        1,
        0,
        1,
        0,
    ]
    recent_expected_spans = sorted(
        {name for group in diagnostic_groups.values() for name in group}
    )
    report = {
        "passed": bool(
            trace_id
            and not missing
            and advisory_content_visible
            and all_agent_io_visible
            and not transport_runs
            and action_complete
            and duplicate_ticks_suppressed
        ),
        "synthetic_fixture": pdf.name,
        "trace_id_returned": bool(trace_id),
        "trace_selection": trace_selection,
        "returned_w3c_id_direct_match": selected_trace_id == trace_id,
        "single_trace_verified": bool(matching_runs) and not missing,
        "spans_verified": sorted(EXPECTED_SPANS & names),
        "missing_spans": missing,
        "eval_prompt_input_output_visible": advisory_content_visible,
        "agent_input_output_visible": agent_io_visible,
        "all_agent_input_output_visible": all_agent_io_visible,
        "generic_http_transport_runs": transport_runs,
        "duplicate_ticks_suppressed": duplicate_ticks_suppressed,
        "notification_count": int(bool(one_consequence and consequences[0].get("notification_id"))),
        "search_count": int(bool(one_consequence and consequences[0].get("search_id"))),
        "query_status": query_status,
        "recent_expected_spans": recent_expected_spans,
        "recent_trace_group_span_counts": sorted(
            (len(group) for group in diagnostic_groups.values()), reverse=True
        ),
        "recent_trace_groups": sorted(
            (sorted(group) for group in diagnostic_groups.values()),
            key=lambda group: (-len(group), group),
        ),
        "returned_trace_recent_span_count": len(
            diagnostic_groups.get(trace_id, set())
        ),
        "trace_content_printed": False,
    }
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
