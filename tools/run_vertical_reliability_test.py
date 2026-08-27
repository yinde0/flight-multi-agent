from __future__ import annotations

import argparse
import json
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
    / "vertical_08_expected.json"
)
COMPOSE_FILES = (
    "compose.yaml",
    "compose.test.yaml",
    "compose.reliability-test.yaml",
)


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
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_detail = "no response"
    while time.monotonic() < deadline:
        try:
            response = client.get(url)
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


def provider_calls(client: httpx.Client) -> tuple[int, int]:
    notification = client.get(
        "http://127.0.0.1:18007/v1/reliability/audit"
    )
    search = client.get("http://127.0.0.1:18009/v1/reliability/audit")
    notification.raise_for_status()
    search.raise_for_status()
    return (
        int(notification.json()["provider_call_count"]),
        int(search.json()["provider_call_count"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prove DynamoDB outbox recovery, JetStream persistence, and "
            "exactly-once traveler consequences across staged outages."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--startup-timeout", type=float, default=120)
    args = parser.parse_args()

    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    run_id = uuid.uuid4().hex[:12]
    request = {
        "trip_id": f"trip-v8-{run_id}",
        "leg_id": f"leg-v8-{run_id}",
        "flight_iata": "NB204",
        "flight_date": "2026-09-15",
        "replay_key": f"vertical-08-{run_id}",
    }
    monitor_audit_url = "http://127.0.0.1:18004/v1/reliability/outbox"
    eval_base = "http://127.0.0.1:18005"
    errors: list[str] = []
    observed: dict[str, Any] = {}

    with httpx.Client(timeout=20, trust_env=False) as client:
        wait_json(
            client,
            f"{args.base_url.rstrip('/')}/health/live",
            lambda payload: payload.get("status") == "ok",
            timeout_seconds=args.startup_timeout,
        )
        wait_json(
            client,
            monitor_audit_url,
            lambda payload: "candidate_outbox_pending" in payload,
        )
        initial_candidate_outbox = int(
            client.get(monitor_audit_url).json()["candidate_outbox_pending"]
        )
        initial_notification_calls, initial_search_calls = provider_calls(client)

        try:
            baseline = post_poll(client, args.base_url, request)
            observed["baseline_status"] = baseline.get("status")

            compose(
                "stop",
                "eval-agent",
                "notification-action-service",
                "flight-search-action-service",
            )
            compose("stop", "nats")

            outage = post_poll(client, args.base_url, request)
            candidate = outage.get("candidate") or {}
            candidate_id = str(candidate.get("candidate_id") or "")
            if not candidate_id:
                raise RuntimeError("Outage poll did not retain its candidate")
            observed["outage_poll_status"] = outage.get("status")
            observed["outage_error_code"] = outage.get("error_code")

            during_outage = wait_json(
                client,
                monitor_audit_url,
                lambda payload: int(payload["candidate_outbox_pending"])
                >= initial_candidate_outbox + 1,
            )
            observed["candidate_outbox_during_outage"] = (
                int(during_outage["candidate_outbox_pending"])
                - initial_candidate_outbox
            )

            compose("up", "-d", "--wait", "--no-deps", "nats")
            after_publish = wait_json(
                client,
                monitor_audit_url,
                lambda payload: int(payload["candidate_outbox_pending"])
                == initial_candidate_outbox,
            )
            observed["candidate_outbox_after_recovery"] = (
                int(after_publish["candidate_outbox_pending"])
                - initial_candidate_outbox
            )

            # Restart after publication: the candidate now has to survive solely
            # in JetStream's file-backed stream.
            compose("restart", "nats")
            compose("up", "-d", "--wait", "--no-deps", "nats")
            compose("up", "-d", "--wait", "--no-deps", "eval-agent")

            event_url = f"{eval_base}/v1/reliability/events/{candidate_id}"
            evaluated = wait_json(
                client,
                event_url,
                lambda payload: payload.get("decision") is not None
                and payload.get("confirmed_event") is not None,
            )
            observed["decision_verdict"] = evaluated["decision"]["verdict"]

            compose(
                "up",
                "-d",
                "--wait",
                "--no-deps",
                "notification-action-service",
                "flight-search-action-service",
            )
            completed = wait_json(
                client,
                event_url,
                lambda payload: (
                    (payload.get("notification") or {}).get("status")
                    == "delivered"
                    and (payload.get("search") or {}).get("status")
                    == "completed"
                ),
            )
            observed["notification_status"] = completed["notification"]["status"]
            observed["search_status"] = completed["search"]["status"]

            calls_after_actions = provider_calls(client)
            observed["notification_provider_call_delta"] = (
                calls_after_actions[0] - initial_notification_calls
            )
            observed["search_provider_call_delta"] = (
                calls_after_actions[1] - initial_search_calls
            )

            redelivery = client.post(
                f"{event_url}/redeliver",
                json={"delivery_id": f"forced-{run_id}"},
            )
            redelivery.raise_for_status()
            wait_json(
                client,
                f"{eval_base}/v1/reliability/bus",
                lambda payload: all(
                    (payload["consumers"].get(name) or {}).get("pending") == 0
                    and (payload["consumers"].get(name) or {}).get("ack_pending")
                    == 0
                    for name in (
                        "travel-notification-action-v1",
                        "travel-flight-search-action-v1",
                    )
                ),
            )

            compose(
                "restart",
                "notification-action-service",
                "flight-search-action-service",
            )
            compose(
                "up",
                "-d",
                "--wait",
                "--no-deps",
                "notification-action-service",
                "flight-search-action-service",
            )
            final_calls = provider_calls(client)
            if final_calls != calls_after_actions:
                errors.append("redelivery or restart repeated an external provider call")

            final_audit = wait_json(
                client,
                event_url,
                lambda payload: payload.get("decision") is not None,
            )
            observed["dead_letter_total"] = sum(
                int(value)
                for value in final_audit.get("dead_letter_counts", {}).values()
            )
            observed["outbox_pending_total"] = sum(
                int(value)
                for value in final_audit.get("outbox_pending", {}).values()
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
        errors.append("observed reliability invariants differ from golden")
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
