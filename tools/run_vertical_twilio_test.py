from __future__ import annotations

import hashlib
import json
import time
import uuid

from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "output" / "pdf" / "synthetic_direct_eticket.pdf"
SYNTHETIC_PHONE = "+447700900123"


def wait_for_health(
    client: httpx.Client,
    url: str,
    *,
    timeout_seconds: float = 120,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if client.get(url).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Service did not become healthy: {url}")


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    deadline = time.monotonic() + 60
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            response = client.request(method, url, **kwargs)
            if response.status_code < 500:
                return response
            last_error = f"HTTP {response.status_code}"
        except httpx.TransportError as error:
            last_error = str(error)
        time.sleep(0.5)
    raise RuntimeError(f"Request failed: {last_error}")


def main() -> int:
    base_url = "http://127.0.0.1:8080"
    stub_url = "http://127.0.0.1:18013"
    run_id = uuid.uuid4().hex[:12]
    trip_id = f"trip-v13-{run_id}"
    content = PDF.read_bytes()

    with httpx.Client(timeout=90, trust_env=False) as client:
        wait_for_health(client, f"{base_url}/health/live")
        wait_for_health(client, f"{stub_url}/health/live")
        client.post(f"{stub_url}/v1/test/reset").raise_for_status()

        activation = request_with_retry(
            client,
            "POST",
            f"{base_url}/v1/trips/activate",
            files={"file": (PDF.name, content, "application/pdf")},
            data={
                "trip_id": trip_id,
                "traveler_ref": "traveler-synthetic-twilio",
                "fixture_id": f"doc-v13-{run_id}",
                "phone_e164": SYNTHETIC_PHONE,
                "sms_consent": "true",
            },
        )
        activation.raise_for_status()

        outcomes: list[dict[str, Any]] = []
        for now in ("2026-09-15T06:00:00Z", "2026-09-15T06:10:00Z"):
            response = request_with_retry(
                client,
                "POST",
                f"{base_url}/v1/orchestration/tick",
                json={"now": now, "maximum_legs": 10},
            )
            response.raise_for_status()
            outcomes.append(response.json())

        trip_response = client.get(f"{base_url}/v1/trips/{trip_id}")
        trip_response.raise_for_status()
        public_trip = trip_response.json()
        private_route_status = client.get(
            f"{base_url}/v1/trips/{trip_id}/notification-recipient"
        ).status_code
        audit = client.get(f"{stub_url}/v1/test/audit").json()

    consequence = next(
        (
            result
            for outcome in outcomes
            for result in outcome.get("results", [])
            if result.get("trip_id") == trip_id
            and result.get("verdict") == "NOTIFY_AND_SEARCH"
        ),
        {},
    )
    public_payload = json.dumps(public_trip, sort_keys=True)
    expected_phone_hash = hashlib.sha256(
        SYNTHETIC_PHONE.encode("utf-8")
    ).hexdigest()
    body = str(audit.get("last_body") or "")
    checks = {
        "trip_activated": activation.json().get("status") == "activated",
        "eval_approved_cancellation": consequence.get("category") == "CANCELLATION",
        "twilio_submission_accepted": consequence.get("notification_status")
        == "accepted",
        "one_sms_submitted": audit.get("message_count") == 1,
        "api_key_auth_valid": audit.get("auth_valid") is True,
        "account_and_sender_valid": audit.get("account_valid") is True
        and audit.get("sender_valid") is True,
        "recipient_matches_consented_phone": audit.get("last_to_sha256")
        == expected_phone_hash,
        "phone_absent_from_public_trip": SYNTHETIC_PHONE not in public_payload,
        "private_recipient_not_publicly_routed": private_route_status == 404,
        "sms_contains_customer_safe_disruption": "cancelled" in body.lower()
        and trip_id not in body,
    }
    report = {
        "slice": "13-consented-twilio-sms",
        "passed": all(checks.values()),
        "trip_id": trip_id,
        "checks": checks,
        "provider": "twilio-stub",
        "provider_message_count": audit.get("message_count"),
    }
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
