from __future__ import annotations

import argparse
import json
import subprocess
import uuid

from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]


def discover_via_internal_mcp() -> dict[str, Any]:
    """Run discovery inside Docker so the API key never enters this process."""
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "monitor-agent",
            "python",
            "-m",
            "flight_agent.live_discovery_cli",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Live discovery failed inside the MCP boundary; inspect sanitized "
            "flight-status-mcp logs for the provider error code"
        )
    for line in reversed(completed.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("Live discovery returned no structured sample")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Perform one live monitoring poll through the public Travel API."
    )
    parser.add_argument(
        "flight_iata",
        nargs="?",
        help="Optional known flight, for example BA117. Omit to discover one.",
    )
    parser.add_argument(
        "flight_date",
        nargs="?",
        help="Required with flight_iata. Omit both to discover a live sample.",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()

    if bool(args.flight_iata) != bool(args.flight_date):
        parser.error("provide both flight_iata and flight_date, or omit both")

    sample = None
    if args.flight_iata and args.flight_date:
        flight_iata = args.flight_iata.upper()
        flight_date = args.flight_date
        discovery_mode = "caller-provided"
    else:
        sample = discover_via_internal_mcp()
        flight_iata = str(sample["flight_iata"])
        flight_date = str(sample["flight_date"])
        discovery_mode = "unfiltered-live-feed"

    suffix = uuid.uuid4().hex[:12]
    request = {
        "trip_id": f"trip-live-{suffix}",
        "leg_id": f"leg-live-{suffix}",
        "flight_iata": flight_iata,
        "flight_date": flight_date,
    }
    with httpx.Client(timeout=60, trust_env=False) as client:
        response = client.post(
            f"{args.base_url.rstrip('/')}/v1/monitoring/poll", json=request
        )
        response.raise_for_status()
        outcome = response.json()

    # Provider credentials never appear in the canonical monitoring outcome.
    report: dict[str, Any] = {
        "discovery_mode": discovery_mode,
        "selected_flight": {
            "flight_iata": flight_iata,
            "flight_date": flight_date,
        },
        "monitoring_outcome": outcome,
    }
    if sample:
        report["selected_flight"].update(
            {
                "origin": sample["origin"],
                "destination": sample["destination"],
                "status": sample["observation"]["status"],
            }
        )
    print(json.dumps(report, indent=2))
    return 0 if outcome.get("status") != "poll_failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
