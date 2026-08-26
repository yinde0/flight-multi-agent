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
FIXTURES = ROOT / "travel_eval" / "fixtures" / "monitoring"
COMPOSE_FILES = [
    "compose.yaml",
    "compose.test.yaml",
    "compose.weather-test.yaml",
]


def wait_for_api(client: httpx.Client, base_url: str, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    health_url = f"{base_url.rstrip('/')}/health/live"
    while time.monotonic() < deadline:
        try:
            response = client.get(health_url)
            if response.status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise RuntimeError("Travel API did not become ready before the timeout")


def post_poll_with_retry(
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
    raise RuntimeError(f"Weather monitoring poll failed before timeout: {last_detail}")


def decision_view(outcome: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {
        "status": outcome["status"],
        "weather_status": outcome["orchestration"]["weather_evidence"]["status"],
    }
    candidate = outcome.get("candidate")
    decision = outcome.get("decision")
    if candidate and decision:
        view.update(
            {
                "category": candidate["category"],
                "weather_risk_level": candidate["weather_risk_level"],
                "delay_minutes": candidate["delay_minutes"],
                "corroborated": candidate["corroborated_by_weather"],
                "verdict": decision["verdict"],
                "reason_codes": decision["reason_codes"],
                "confirmed": "confirmed_event" in outcome,
            }
        )
    else:
        view["candidate_published"] = bool(candidate)
    return view


def restart_monitor() -> None:
    command = ["docker", "compose"]
    for filename in COMPOSE_FILES:
        command.extend(["-f", filename])
    command.extend(["restart", "monitor-agent"])
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay and judge the containerized weather-corroboration slice."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--startup-timeout", type=float, default=120)
    parser.add_argument(
        "--restart-monitor",
        action="store_true",
        help="Restart the Monitoring Agent after baseline to prove durable state.",
    )
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:12]
    poll_request = {
        "trip_id": f"trip-v4-{run_id}",
        "leg_id": f"leg-v4-{run_id}",
        "flight_iata": "NB204",
        "flight_date": "2026-09-15",
        "replay_key": f"vertical-04-{run_id}",
    }
    expected = json.loads(
        (FIXTURES / "vertical_04_expected.json").read_text(encoding="utf-8")
    )["polls"]
    outcomes: list[dict[str, Any]] = []

    with httpx.Client(timeout=60, trust_env=False) as client:
        wait_for_api(client, args.base_url, timeout_seconds=args.startup_timeout)
        url = f"{args.base_url.rstrip('/')}/v1/monitoring/poll"
        outcomes.append(post_poll_with_retry(client, url, poll_request))
        if args.restart_monitor:
            restart_monitor()
        for _ in range(6):
            outcomes.append(post_poll_with_retry(client, url, poll_request))

    observed = [decision_view(item) for item in outcomes]
    passed = observed == expected
    report = {
        "passed": passed,
        "state_persistence_check": (
            "monitor-agent restarted after baseline"
            if args.restart_monitor
            else "restart not requested"
        ),
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
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
