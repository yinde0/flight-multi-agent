from __future__ import annotations

import argparse
import json
import time
import uuid

from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "travel_eval" / "fixtures" / "monitoring"


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


def post_poll(
    client: httpx.Client,
    url: str,
    request: dict[str, str],
    *,
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
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
    raise RuntimeError(f"Notification vertical poll failed: {last_detail}")


def decision_view(outcome: dict[str, Any]) -> dict[str, Any]:
    notification_status = outcome["orchestration"].get(
        "notification_action", {"status": "not_required"}
    )["status"]
    view: dict[str, Any] = {
        "status": outcome["status"],
    }
    candidate = outcome.get("candidate")
    decision = outcome.get("decision")
    notification = outcome.get("notification")
    if candidate and decision:
        view.update(
            {
                "category": candidate["category"],
                "verdict": decision["verdict"],
                "confirmed": "confirmed_event" in outcome,
                "notification_status": notification_status,
                "notification_recorded": notification is not None,
            }
        )
        if notification is not None:
            view["provider"] = notification["provider"]
    else:
        view.update(
            {
                "candidate_published": bool(candidate),
                "notification_status": notification_status,
            }
        )
    return view


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Judge the post-Eval notification authority vertical slice."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--startup-timeout", type=float, default=120)
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:12]
    request = {
        "trip_id": f"trip-v5-{run_id}",
        "leg_id": f"leg-v5-{run_id}",
        "flight_iata": "NB204",
        "flight_date": "2026-09-15",
        "replay_key": f"vertical-05-{run_id}",
    }
    expected = json.loads(
        (FIXTURES / "vertical_05_expected.json").read_text(encoding="utf-8")
    )["polls"]

    with httpx.Client(timeout=60, trust_env=False) as client:
        wait_for_api(client, args.base_url, timeout_seconds=args.startup_timeout)
        url = f"{args.base_url.rstrip('/')}/v1/monitoring/poll"
        outcomes = [post_poll(client, url, request) for _ in range(7)]

    observed = [decision_view(item) for item in outcomes]
    passed = observed == expected
    notification_ids = [
        item["notification"]["notification_id"]
        for item in outcomes
        if item.get("notification")
    ]
    report = {
        "passed": passed and len(notification_ids) == len(set(notification_ids)) == 1,
        "notification_count": len(notification_ids),
        "unique_notification_count": len(set(notification_ids)),
        "polls": [
            {
                "number": index,
                "passed": actual == wanted,
                "observed": actual,
                "expected": wanted,
            }
            for index, (actual, wanted) in enumerate(
                zip(observed, expected, strict=True), start=1
            )
        ],
    }
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
