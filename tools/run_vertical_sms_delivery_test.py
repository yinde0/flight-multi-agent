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
    client: httpx.Client, url: str, *, timeout_seconds: float = 120
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


def trigger_callback(
    client: httpx.Client,
    stub_url: str,
    message_sid: str,
    message_status: str,
    *,
    valid_signature: bool = True,
) -> int:
    response = client.post(
        f"{stub_url}/v1/test/callback",
        json={
            "message_sid": message_sid,
            "message_status": message_status,
            "valid_signature": valid_signature,
        },
    )
    response.raise_for_status()
    return int(response.json()["callback_http_status"])


def main() -> int:
    base_url = "http://127.0.0.1:8080"
    stub_url = "http://127.0.0.1:18013"
    webhook_url = "http://127.0.0.1:18014"
    run_id = uuid.uuid4().hex[:12]
    trip_id = f"trip-v14-{run_id}"
    content = PDF.read_bytes()

    with httpx.Client(timeout=90, trust_env=False) as client:
        wait_for_health(client, f"{base_url}/health/live")
        wait_for_health(client, f"{stub_url}/health/live")
        wait_for_health(client, f"{webhook_url}/health/live")
        client.post(f"{stub_url}/v1/test/reset").raise_for_status()

        activation = request_with_retry(
            client,
            "POST",
            f"{base_url}/v1/trips/activate",
            files={"file": (PDF.name, content, "application/pdf")},
            data={
                "trip_id": trip_id,
                "traveler_ref": "traveler-synthetic-sms-delivery",
                "fixture_id": f"doc-v14-{run_id}",
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

        audit = client.get(f"{stub_url}/v1/test/audit").json()
        message_sid = str(audit.get("last_message_sid") or "")
        initial_delivery_response = client.get(
            f"{webhook_url}/v1/test/deliveries/{message_sid}"
        )
        initial_delivery_response.raise_for_status()
        initial_delivery = initial_delivery_response.json()

        callback_statuses = {
            "sent": trigger_callback(client, stub_url, message_sid, "sent"),
            "delivered": trigger_callback(
                client, stub_url, message_sid, "delivered"
            ),
            "duplicate_delivered": trigger_callback(
                client, stub_url, message_sid, "delivered"
            ),
            "stale_sent": trigger_callback(
                client, stub_url, message_sid, "sent"
            ),
            "forged_failed": trigger_callback(
                client,
                stub_url,
                message_sid,
                "failed",
                valid_signature=False,
            ),
        }
        final_delivery = client.get(
            f"{webhook_url}/v1/test/deliveries/{message_sid}"
        ).json()
        public_trip = client.get(f"{base_url}/v1/trips/{trip_id}").json()

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
    checks = {
        "eval_approved_cancellation": consequence.get("category")
        == "CANCELLATION",
        "one_sms_submitted": audit.get("message_count") == 1,
        "status_callback_supplied": audit.get("status_callback_supplied") is True,
        "recipient_matches_consent": audit.get("last_to_sha256")
        == expected_phone_hash,
        "initial_state_is_queued": initial_delivery.get("status") == "accepted"
        and initial_delivery.get("provider_status") == "queued",
        "signed_callbacks_accepted": all(
            callback_statuses[name] == 204
            for name in (
                "sent",
                "delivered",
                "duplicate_delivered",
                "stale_sent",
            )
        ),
        "forged_callback_rejected": callback_statuses["forged_failed"] == 403,
        "terminal_delivery_preserved": final_delivery.get("status") == "delivered"
        and final_delivery.get("provider_status") == "delivered"
        and final_delivery.get("error_code") is None,
        "phone_absent_from_public_trip": SYNTHETIC_PHONE not in public_payload,
        "phone_absent_from_delivery_record": SYNTHETIC_PHONE
        not in json.dumps(final_delivery, sort_keys=True),
    }
    report = {
        "slice": "14-signed-sms-delivery-reconciliation",
        "passed": all(checks.values()),
        "trip_id": trip_id,
        "checks": checks,
        "callback_http_statuses": callback_statuses,
        "final_provider_status": final_delivery.get("provider_status"),
    }
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
