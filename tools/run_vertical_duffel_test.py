from __future__ import annotations

import argparse
import json
import time
import uuid

from typing import Any

import httpx


def wait_for_api(client: httpx.Client, base_url: str, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if client.get(f"{base_url.rstrip('/')}/health/live").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise RuntimeError("Travel API did not become ready before timeout")


def post_poll(client: httpx.Client, url: str, request: dict[str, str]) -> dict[str, Any]:
    deadline = time.monotonic() + 90
    last_detail = "no response"
    while time.monotonic() < deadline:
        try:
            response = client.post(url, json=request)
            if response.status_code == 200 and isinstance(response.json(), dict):
                return response.json()
            last_detail = f"HTTP {response.status_code}: {response.text[:300]}"
        except httpx.TransportError as error:
            last_detail = str(error)
        time.sleep(0.5)
    raise RuntimeError(f"Duffel vertical poll failed: {last_detail}")


def validate_search(search: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    scope = search.get("source_scope")
    expected_status = {
        "provider_test_offers": "provider_test_offer",
        "live_offers": "live_offer",
    }.get(scope)
    if expected_status is None:
        errors.append(f"unexpected source_scope: {scope}")
    if search.get("availability_verified") is not (scope == "live_offers"):
        errors.append("availability_verified does not match Duffel live_mode")
    if search.get("booking_guaranteed") is not False:
        errors.append("booking_guaranteed must remain false")
    if search.get("booking_authorized") is not False:
        errors.append("booking_authorized must remain false")

    alternatives = search.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        errors.append("no feasible priced alternatives were returned")
        return errors
    for alternative in alternatives:
        price = alternative.get("price")
        if not isinstance(price, dict) or not price.get("amount") or not price.get("currency"):
            errors.append(f"{alternative.get('option_id')}: missing normalized price")
        if not alternative.get("offer_expires_at"):
            errors.append(f"{alternative.get('option_id')}: missing offer expiry")
        if alternative.get("availability_status") != expected_status:
            errors.append(
                f"{alternative.get('option_id')}: availability status/mode mismatch"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exercise cancellation-to-Duffel offer search with live HTTP calls."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--startup-timeout", type=float, default=120)
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:12]
    request = {
        "trip_id": f"trip-duffel-{run_id}",
        "leg_id": f"leg-duffel-{run_id}",
        "flight_iata": "NB204",
        "flight_date": "2026-09-15",
        "replay_key": f"vertical-06-duffel-{run_id}",
    }
    with httpx.Client(timeout=90, trust_env=False) as client:
        wait_for_api(client, args.base_url, timeout_seconds=args.startup_timeout)
        url = f"{args.base_url.rstrip('/')}/v1/monitoring/poll"
        outcomes = [post_poll(client, url, request) for _ in range(3)]

    middle = outcomes[1]
    search = middle.get("search") or {}
    errors = validate_search(search)
    expected_states = ["baseline_stored", "candidate_evaluated", "unchanged"]
    observed_states = [outcome.get("status") for outcome in outcomes]
    if observed_states != expected_states:
        errors.append(
            f"poll states differ: expected {expected_states}, observed {observed_states}"
        )
    if middle.get("decision", {}).get("verdict") != "NOTIFY_AND_SEARCH":
        errors.append("cancellation did not receive NOTIFY_AND_SEARCH")
    if middle.get("orchestration", {}).get("notification_action", {}).get("status") != "delivered":
        errors.append("approved notification was not delivered")
    if middle.get("orchestration", {}).get("search_action", {}).get("status") != "completed":
        errors.append("authorized Duffel search did not complete")

    notification_ids = [
        outcome["notification"]["notification_id"]
        for outcome in outcomes
        if outcome.get("notification")
    ]
    search_ids = [
        outcome["search"]["search_id"]
        for outcome in outcomes
        if outcome.get("search")
    ]
    if len(notification_ids) != len(set(notification_ids)) or len(notification_ids) != 1:
        errors.append("notification was not exactly-once across the replay")
    if len(search_ids) != len(set(search_ids)) or len(search_ids) != 1:
        errors.append("search was not exactly-once across the replay")

    alternatives = search.get("alternatives") or []
    report = {
        "passed": not errors,
        "errors": errors,
        "poll_states": observed_states,
        "provider": search.get("provider"),
        "source_scope": search.get("source_scope"),
        "availability_verified": search.get("availability_verified"),
        "booking_guaranteed": search.get("booking_guaranteed"),
        "booking_authorized": search.get("booking_authorized"),
        "alternative_count": len(alternatives),
        "top_alternatives": [
            {
                "route": [
                    f"{segment['origin']}-{segment['destination']}"
                    for segment in alternative["segments"]
                ],
                "price": alternative["price"],
                "offer_expires_at": alternative["offer_expires_at"],
                "availability_status": alternative["availability_status"],
            }
            for alternative in alternatives[:3]
        ],
        "notification_count": len(notification_ids),
        "search_count": len(search_ids),
    }
    print(json.dumps(report, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
