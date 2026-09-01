from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import uuid

from pathlib import Path
from typing import Any, Callable

import httpx


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = (
    ROOT
    / "travel_eval"
    / "fixtures"
    / "monitoring"
    / "vertical_09_expected.json"
)
COMPOSE_FILES = (
    "compose.yaml",
    "compose.test.yaml",
    "compose.operations-test.yaml",
)
OPS_TOKEN = "slice09-test-token"
NOTIFICATION_CONSUMER = "travel-notification-action-v1"


def compose(*arguments: str) -> None:
    command = ["docker", "compose"]
    for path in COMPOSE_FILES:
        command.extend(["-f", path])
    command.extend(arguments)
    subprocess.run(command, cwd=ROOT, check=True)


def wait_json(
    client: httpx.Client,
    url: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_detail = "no response"
    while time.monotonic() < deadline:
        try:
            response = client.get(url, headers=headers)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and predicate(payload):
                    return payload
                last_detail = json.dumps(payload)[:300]
            else:
                last_detail = f"HTTP {response.status_code}: {response.text[:200]}"
        except httpx.TransportError as error:
            last_detail = str(error)
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {url}: {last_detail}")


def post_poll(
    client: httpx.Client, base_url: str, request: dict[str, str]
) -> dict[str, Any]:
    response = client.post(
        f"{base_url.rstrip('/')}/v1/monitoring/poll",
        json=request,
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Monitoring poll did not return a JSON object")
    return payload


def metric_value(text: str, metric: str, labels: dict[str, str]) -> int:
    rendered = ",".join(f'{key}="{value}"' for key, value in labels.items())
    match = re.search(
        rf"^{re.escape(metric)}\{{{re.escape(rendered)}\}}\s+([0-9]+)$",
        text,
        re.MULTILINE,
    )
    return int(match.group(1)) if match else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prove observable quarantine, authenticated re-drive, OTLP export, "
            "and idempotent recovery."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--startup-timeout", type=float, default=120)
    args = parser.parse_args()

    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    run_id = uuid.uuid4().hex[:12]
    poll_request = {
        "trip_id": f"trip-v9-{run_id}",
        "leg_id": f"leg-v9-{run_id}",
        "flight_iata": "NB204",
        "flight_date": "2026-09-15",
        "replay_key": f"vertical-09-{run_id}",
    }
    ops_base = "http://127.0.0.1:18012"
    eval_base = "http://127.0.0.1:18005"
    travel_tools_base = "http://127.0.0.1:18003"
    action_metrics = "http://127.0.0.1:18008/metrics"
    otel_audit = "http://127.0.0.1:14318/v1/telemetry/audit"
    headers = {"x-ops-token": OPS_TOKEN}
    observed: dict[str, Any] = {}
    errors: list[str] = []

    with httpx.Client(timeout=20, trust_env=False) as client:
        wait_json(
            client,
            f"{args.base_url.rstrip('/')}/health/live",
            lambda payload: payload.get("status") == "ok",
            timeout_seconds=args.startup_timeout,
        )
        wait_json(
            client,
            f"{ops_base}/health/live",
            lambda payload: payload.get("status") == "ok",
        )
        inject_outage = client.post(
            f"{travel_tools_base}/v1/operations/notification/failure-mode",
            headers=headers,
            json={"enabled": True},
        )
        inject_outage.raise_for_status()
        initial_provider = wait_json(
            client,
            f"{travel_tools_base}/v1/reliability/audit",
            lambda payload: (
                payload.get("notification", {}).get("failure_mode_enabled") is True
            ),
        )
        initial_notification = initial_provider["notification"]
        initial_calls = int(initial_notification["provider_call_count"])
        initial_unique = int(initial_notification["unique_delivery_count"])
        initial_action_metrics = client.get(action_metrics)
        initial_action_metrics.raise_for_status()
        initial_failed_actions = metric_value(
            initial_action_metrics.text,
            "travel_operation_executions_total",
            {
                "operation": "agent.orchestrator.notify_traveler",
                "outcome": "failed",
            },
        )

        try:
            baseline = post_poll(client, args.base_url, poll_request)
            if baseline.get("status") != "baseline_stored":
                errors.append("baseline was not stored")
            cancellation = post_poll(client, args.base_url, poll_request)
            candidate = cancellation.get("candidate") or {}
            decision = cancellation.get("decision") or {}
            candidate_id = str(candidate.get("candidate_id") or "")
            decision_id = str(decision.get("decision_id") or "")
            if not candidate_id or not decision_id:
                raise RuntimeError("Cancellation did not produce a decision")

            dead_letter_url = (
                f"{ops_base}/v1/operations/dead-letters/"
                f"{NOTIFICATION_CONSUMER}"
            )
            quarantined = wait_json(
                client,
                dead_letter_url,
                lambda payload: any(
                    event.get("event_id") == decision_id
                    for event in payload.get("events", [])
                ),
                headers=headers,
            )
            record = next(
                event
                for event in quarantined["events"]
                if event["event_id"] == decision_id
            )
            observed["retry_count"] = int(record["attempts"])
            observed["active_dead_letters_before_recovery"] = 1

            before_recovery = client.get(
                f"{travel_tools_base}/v1/reliability/audit"
            )
            before_recovery.raise_for_status()
            before_payload = before_recovery.json()["notification"]
            observed["notification_provider_calls_before_recovery"] = (
                int(before_payload["provider_call_count"]) - initial_calls
            )

            action_response = client.get(action_metrics)
            action_response.raise_for_status()
            observed["failed_notification_operations"] = metric_value(
                action_response.text,
                "travel_operation_executions_total",
                {
                    "operation": "agent.orchestrator.notify_traveler",
                    "outcome": "failed",
                },
            ) - initial_failed_actions

            redrive_url = f"{dead_letter_url}/{decision_id}/redrive"
            command = {
                "request_id": f"redrive-v9-{run_id}",
                "operator_ref": "operator:vertical-09",
                "reason": "Notification capability recovered after injected outage.",
            }
            unauthorized = client.post(redrive_url, json=command)
            observed["unauthorized_redrive_status"] = unauthorized.status_code

            repair = client.post(
                f"{travel_tools_base}/v1/operations/notification/failure-mode",
                headers=headers,
                json={"enabled": False},
            )
            repair.raise_for_status()

            redrive = client.post(redrive_url, headers=headers, json=command)
            redrive.raise_for_status()
            observed["redrive_status"] = redrive.json()["status"]

            event_url = f"{eval_base}/v1/reliability/events/{candidate_id}"
            recovered = wait_json(
                client,
                event_url,
                lambda payload: (
                    (payload.get("notification") or {}).get("status")
                    == "delivered"
                    and (payload.get("search") or {}).get("status")
                    == "completed"
                ),
            )
            observed["notification_status"] = recovered["notification"]["status"]
            observed["search_status"] = recovered["search"]["status"]

            duplicate = client.post(redrive_url, headers=headers, json=command)
            duplicate.raise_for_status()
            observed["duplicate_redrive_status"] = duplicate.json()["status"]

            final_provider = client.get(
                f"{travel_tools_base}/v1/reliability/audit"
            )
            final_provider.raise_for_status()
            final_payload = final_provider.json()["notification"]
            observed["notification_provider_call_delta"] = (
                int(final_payload["provider_call_count"]) - initial_calls
            )
            observed["notification_unique_delivery_delta"] = (
                int(final_payload["unique_delivery_count"]) - initial_unique
            )

            final_status = wait_json(
                client,
                f"{ops_base}/v1/operations/status",
                lambda payload: (
                    int(payload["dead_letters_active"][NOTIFICATION_CONSUMER]) == 0
                ),
                headers=headers,
            )
            observed["active_dead_letters_after_recovery"] = int(
                final_status["dead_letters_active"][NOTIFICATION_CONSUMER]
            )
            observed["outbox_pending_total"] = sum(
                int(value) for value in final_status["outbox_pending"].values()
            )

            trace_audit = wait_json(
                client,
                otel_audit,
                lambda payload: int(payload.get("accepted_trace_batches", 0)) > 0,
                timeout_seconds=30,
            )
            observed["otel_trace_exported"] = (
                int(trace_audit["accepted_trace_batches"]) > 0
                and int(trace_audit["accepted_trace_bytes"]) > 0
            )
        except Exception as error:
            errors.append(str(error))
        finally:
            try:
                compose("up", "-d", "--wait", "--remove-orphans")
            except Exception as restore_error:
                errors.append(f"stack restoration failed: {restore_error}")

    expected_view = {
        key: value
        for key, value in expected.items()
        if key not in {"schema_version", "description"}
    }
    if observed != expected_view:
        errors.append("observed operations invariants differ from golden")
    report = {
        "passed": not errors,
        "errors": errors,
        "observed": observed,
        "expected": expected_view,
    }
    print(json.dumps(report, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
