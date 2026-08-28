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
EXPECTED = (
    ROOT
    / "travel_eval"
    / "fixtures"
    / "communication"
    / "vertical_17_expected.json"
)


def wait_for_health(client: httpx.Client, url: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if client.get(url).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Service did not become healthy: {url}")


def result_view(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results")
    result = results[0] if isinstance(results, list) and results else {}
    return {
        "monitoring_status": result.get("monitoring_status"),
        "category": result.get("category"),
        "verdict": result.get("verdict"),
        "notification_status": result.get("notification_status"),
        "notification_message": result.get("notification_message"),
    }


def main() -> int:
    api_url = os.getenv("TRAVEL_API_URL", "http://127.0.0.1:8080").rstrip("/")
    agency_url = os.getenv(
        "FLIGHT_AGENCY_DEMO_URL", "http://127.0.0.1:18015"
    ).rstrip("/")
    token = os.getenv("FLIGHT_AGENCY_DEMO_TOKEN") or "local-flight-agency-demo"
    run_id = uuid.uuid4().hex[:12]
    trip_id = f"trip-friendly-{run_id}"
    headers = {"X-Flight-Agency-Token": token}

    with httpx.Client(timeout=120, trust_env=False) as client:
        wait_for_health(client, f"{api_url}/health/live")
        wait_for_health(client, f"{agency_url}/health/live")
        client.delete(f"{agency_url}/v1/flights", headers=headers).raise_for_status()
        activation = client.post(
            f"{api_url}/v1/trips/activate",
            files={"file": (PDF.name, PDF.read_bytes(), "application/pdf")},
            data={
                "trip_id": trip_id,
                "traveler_ref": f"traveler-{run_id}",
                "fixture_id": f"communication-{run_id}",
            },
        )
        activation.raise_for_status()
        sync = client.post(f"{api_url}/v1/demo/agency/trips/{trip_id}/sync")
        sync.raise_for_status()
        flight = sync.json()["flights"][0]

        baseline = client.post(
            f"{api_url}/v1/demo/agency/trips/{trip_id}/check"
        )
        baseline.raise_for_status()
        change = client.patch(
            f"{agency_url}/v1/flights/{flight['flight_iata']}/{flight['flight_date']}",
            headers=headers,
            json={
                "status": "scheduled",
                "departure_delay_minutes": 45,
                "arrival_delay_minutes": 45,
                "note": "Synthetic friendly-language test",
            },
        )
        change.raise_for_status()
        delayed = client.post(
            f"{api_url}/v1/demo/agency/trips/{trip_id}/check"
        )
        delayed.raise_for_status()

    observed = {
        "baseline": result_view(baseline.json()),
        "material_delay": result_view(delayed.json()),
    }
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    report = {
        "slice": "17-friendly-disruption-explanation",
        "passed": observed == expected,
        "synthetic_only": True,
        "real_azure_calls": 0,
        "authority": "deterministic_eval_policy",
        "observed": observed,
        "expected": expected,
    }
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
