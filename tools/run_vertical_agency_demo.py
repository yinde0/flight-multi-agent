from __future__ import annotations

import json
import os
import time
import uuid

from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "output" / "pdf" / "synthetic_direct_eticket.pdf"


def wait_for_health(client: httpx.Client, url: str, timeout_seconds: float = 120) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if client.get(url).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Service did not become healthy: {url}")


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    timeout_seconds: float = 90,
    **kwargs: Any,
) -> httpx.Response:
    deadline = time.monotonic() + timeout_seconds
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            response = client.request(method, url, **kwargs)
            if response.status_code < 500:
                return response
            last_error = f"HTTP {response.status_code}"
        except httpx.TransportError as error:
            last_error = type(error).__name__
        time.sleep(0.5)
    raise RuntimeError(f"Request did not succeed before timeout: {last_error}")


def result_view(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results")
    result = results[0] if isinstance(results, list) and results else {}
    return {
        "monitoring_status": result.get("monitoring_status"),
        "category": result.get("category"),
        "verdict": result.get("verdict"),
        "notification_status": result.get("notification_status"),
        "search_status": result.get("search_status"),
    }


def main() -> int:
    api_url = os.getenv("TRAVEL_API_URL", "http://127.0.0.1:8080").rstrip("/")
    agency_url = os.getenv(
        "FLIGHT_AGENCY_DEMO_URL", "http://127.0.0.1:18015"
    ).rstrip("/")
    agency_token = os.getenv("FLIGHT_AGENCY_DEMO_TOKEN") or "local-flight-agency-demo"
    run_id = uuid.uuid4().hex[:12]
    trip_id = f"trip-agency-{run_id}"
    headers = {"X-Flight-Agency-Token": agency_token}

    with httpx.Client(timeout=90, trust_env=False) as client:
        wait_for_health(client, f"{api_url}/health/live")
        wait_for_health(client, f"{agency_url}/health/live")
        reset = client.delete(f"{agency_url}/v1/flights", headers=headers)
        reset.raise_for_status()

        activation = request(
            client,
            "POST",
            f"{api_url}/v1/trips/activate",
            files={"file": (PDF.name, PDF.read_bytes(), "application/pdf")},
            data={
                "trip_id": trip_id,
                "traveler_ref": f"traveler-{run_id}",
                "fixture_id": f"agency-demo-{run_id}",
            },
        )
        activation.raise_for_status()

        synced = request(
            client, "POST", f"{api_url}/v1/demo/agency/trips/{trip_id}/sync"
        )
        synced.raise_for_status()
        flight = synced.json()["flights"][0]
        flight_iata = flight["flight_iata"]
        flight_date = flight["flight_date"]

        def check() -> dict[str, Any]:
            response = request(
                client,
                "POST",
                f"{api_url}/v1/demo/agency/trips/{trip_id}/check",
            )
            response.raise_for_status()
            return result_view(response.json())

        def change(payload: dict[str, Any]) -> None:
            response = request(
                client,
                "PATCH",
                f"{api_url}/v1/demo/agency/flights/{flight_iata}/{flight_date}",
                json=payload,
            )
            response.raise_for_status()

        observations: dict[str, dict[str, Any]] = {"baseline": check()}
        change({"departure_gate": "C14", "note": "Demo gate-only change"})
        observations["gate_change"] = check()
        change(
            {
                "status": "scheduled",
                "departure_delay_minutes": 15,
                "arrival_delay_minutes": 15,
                "note": "Demo minor delay",
            }
        )
        observations["minor_delay"] = check()
        change(
            {
                "departure_delay_minutes": 45,
                "arrival_delay_minutes": 45,
                "note": "Demo notify-worthy delay",
            }
        )
        observations["material_delay"] = check()
        change({"status": "cancelled", "note": "Demo cancellation"})
        observations["cancellation"] = check()

        trip_response = client.get(f"{api_url}/v1/trips/{trip_id}")
        trip_response.raise_for_status()
        trip = trip_response.json()
        agency_response = client.get(
            f"{agency_url}/v1/flights/{flight_iata}/{flight_date}",
            headers=headers,
        )
        agency_response.raise_for_status()
        agency_flight = agency_response.json()

    expected = {
        "baseline": {
            "monitoring_status": "baseline_stored",
            "category": None,
            "verdict": None,
            "notification_status": "not_required",
            "search_status": "not_required",
        },
        "gate_change": {
            "monitoring_status": "candidate_evaluated",
            "category": "GATE_CHANGE",
            "verdict": "SUPPRESS",
            "notification_status": "not_required",
            "search_status": "not_required",
        },
        "minor_delay": {
            "monitoring_status": "candidate_evaluated",
            "category": "DELAY",
            "verdict": "SUPPRESS",
            "notification_status": "not_required",
            "search_status": "not_required",
        },
        "material_delay": {
            "monitoring_status": "candidate_evaluated",
            "category": "DELAY",
            "verdict": "NOTIFY",
            "notification_status": "delivered",
            "search_status": "not_required",
        },
        "cancellation": {
            "monitoring_status": "candidate_evaluated",
            "category": "CANCELLATION",
            "verdict": "NOTIFY_AND_SEARCH",
            "notification_status": "delivered",
            "search_status": "completed",
        },
    }
    errors = [
        f"{name} outcome differed from expected"
        for name, expected_view in expected.items()
        if observations.get(name) != expected_view
    ]
    leg = trip.get("legs", [{}])[0]
    if leg.get("poll_count") != 5:
        errors.append("trip did not persist all five monitoring checks")
    if agency_flight.get("revision") != 5:
        errors.append("flight agency did not persist all four operator changes")
    if len(agency_flight.get("history", [])) != 5:
        errors.append("flight agency history is incomplete")

    report = {
        "slice": "15-manual-flight-agency-sandbox",
        "passed": not errors,
        "errors": errors,
        "trip_id": trip_id,
        "flight": f"{flight_iata}/{flight_date}",
        "outcomes": observations,
        "checks_persisted": leg.get("poll_count"),
        "agency_revision": agency_flight.get("revision"),
        "safe_notification_provider": os.getenv(
            "DEMO_NOTIFICATION_PROVIDER", "recording"
        ),
    }
    print(json.dumps(report, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
