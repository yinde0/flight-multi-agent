from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid

from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "output" / "pdf" / "synthetic_direct_eticket.pdf"
EXPECTED = (
    ROOT
    / "travel_eval"
    / "fixtures"
    / "monitoring"
    / "vertical_07_expected.json"
)


def wait_for_api(client: httpx.Client, base_url: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if client.get(f"{base_url.rstrip('/')}/health/live").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise RuntimeError("Travel API did not become ready before timeout")


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    timeout_seconds: float = 60,
    **kwargs: Any,
) -> httpx.Response:
    deadline = time.monotonic() + timeout_seconds
    last_detail = "no response"
    while time.monotonic() < deadline:
        try:
            response = client.request(method, url, **kwargs)
            if response.status_code < 500:
                return response
            last_detail = f"HTTP {response.status_code}: {response.text[:200]}"
        except httpx.TransportError as error:
            last_detail = str(error)
        time.sleep(0.5)
    raise RuntimeError(f"Request failed before timeout: {last_detail}")


def restart_orchestrator() -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "compose.test.yaml",
            "-f",
            "compose.activation-test.yaml",
            "restart",
            "trip-orchestrator",
        ],
        cwd=ROOT,
        check=True,
    )


def tick_view(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload["results"][0] if payload.get("results") else {}
    view = {
        "requested_at": payload["requested_at"],
        "claimed_count": payload["claimed_count"],
        "completed_count": payload["completed_count"],
    }
    if result:
        view["monitoring_status"] = result.get("monitoring_status")
    return view


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Judge PDF-to-S3/Postgres activation and virtual-clock scheduling."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--startup-timeout", type=float, default=120)
    parser.add_argument("--restart-orchestrator", action="store_true")
    args = parser.parse_args()

    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    run_id = uuid.uuid4().hex[:12]
    trip_id = f"trip-v7-{run_id}"
    activation_data = {
        "trip_id": trip_id,
        "traveler_ref": "traveler-synthetic-001",
        "fixture_id": f"doc-v7-{run_id}",
    }
    content = PDF.read_bytes()
    base = args.base_url.rstrip("/")

    with httpx.Client(timeout=90, trust_env=False) as client:
        wait_for_api(client, base, args.startup_timeout)

        def activate() -> dict[str, Any]:
            response = request_with_retry(
                client,
                "POST",
                f"{base}/v1/trips/activate",
                files={"file": (PDF.name, content, "application/pdf")},
                data=activation_data,
            )
            response.raise_for_status()
            return response.json()

        activation = activate()
        repeated_activation = activate()

        tick_payloads = []
        for now in ("2026-09-15T06:00:00Z", "2026-09-15T06:00:00Z"):
            response = request_with_retry(
                client,
                "POST",
                f"{base}/v1/orchestration/tick",
                json={"now": now, "maximum_legs": 10},
            )
            response.raise_for_status()
            tick_payloads.append(response.json())

        if args.restart_orchestrator:
            restart_orchestrator()

        for now in ("2026-09-15T06:10:00Z", "2026-09-15T06:10:00Z"):
            response = request_with_retry(
                client,
                "POST",
                f"{base}/v1/orchestration/tick",
                json={"now": now, "maximum_legs": 10},
            )
            response.raise_for_status()
            tick_payloads.append(response.json())

        trip_response = request_with_retry(
            client, "GET", f"{base}/v1/trips/{trip_id}"
        )
        trip_response.raise_for_status()
        trip = trip_response.json()
        document_response = request_with_retry(
            client, "GET", f"{base}/v1/trips/{trip_id}/document-status"
        )
        document_response.raise_for_status()
        document_status = document_response.json()

    errors: list[str] = []
    activation_view = {
        "status": activation.get("status"),
        "trip_status": activation.get("trip_status"),
        "parse_status": activation.get("parse_status"),
        "active_leg_count": activation.get("active_leg_count"),
        "document_bucket": activation.get("document", {}).get("bucket"),
    }
    repeated_view = {
        "status": repeated_activation.get("status"),
        "idempotent_replay": repeated_activation.get("idempotent_replay"),
        "active_leg_count": repeated_activation.get("active_leg_count"),
    }
    observed_ticks = [tick_view(payload) for payload in tick_payloads]
    if activation_view != expected["activation"]:
        errors.append("initial activation differs from golden")
    if repeated_view != expected["idempotent_activation"]:
        errors.append("repeated activation was not idempotent")
    if observed_ticks != expected["ticks"]:
        errors.append("virtual-clock ticks differ from golden")
    if document_status.get("stored") is not True:
        errors.append("S3 object verification failed")

    leg = trip["legs"][0] if len(trip.get("legs", [])) == 1 else {}
    final_view = {
        "status": trip.get("status"),
        "leg_count": len(trip.get("legs", [])),
        "poll_count": leg.get("poll_count"),
        "last_poll_status": leg.get("last_poll_status"),
        "next_poll_at": leg.get("next_poll_at"),
    }
    if final_view != expected["final_trip"]:
        errors.append("final Postgres trip state differs from golden")

    consequences = [
        result
        for payload in tick_payloads
        for result in payload.get("results", [])
        if result.get("verdict") == "NOTIFY_AND_SEARCH"
    ]
    if len(consequences) != 1:
        errors.append("expected exactly one NOTIFY_AND_SEARCH consequence")
    elif (
        consequences[0].get("category") != "CANCELLATION"
        or consequences[0].get("notification_status") != "delivered"
        or consequences[0].get("search_status") != "completed"
        or not consequences[0].get("notification_id")
        or not consequences[0].get("search_id")
    ):
        errors.append("cancellation consequence was incomplete")

    report = {
        "passed": not errors,
        "errors": errors,
        "trip_id": trip_id,
        "activation": activation_view,
        "idempotent_activation": repeated_view,
        "document_stored": document_status.get("stored"),
        "orchestrator_restarted": args.restart_orchestrator,
        "ticks": observed_ticks,
        "final_trip": final_view,
        "notification_count": sum(
            bool(result.get("notification_id"))
            for payload in tick_payloads
            for result in payload.get("results", [])
        ),
        "search_count": sum(
            bool(result.get("search_id"))
            for payload in tick_payloads
            for result in payload.get("results", [])
        ),
    }
    print(json.dumps(report, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
