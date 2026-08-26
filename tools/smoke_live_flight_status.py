from __future__ import annotations

import argparse
import json
import uuid

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Perform one live monitoring poll through the public Travel API."
    )
    parser.add_argument("flight_iata", help="For example BA117")
    parser.add_argument("flight_date", help="YYYY-MM-DD")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()

    suffix = uuid.uuid4().hex[:12]
    request = {
        "trip_id": f"trip-live-{suffix}",
        "leg_id": f"leg-live-{suffix}",
        "flight_iata": args.flight_iata.upper(),
        "flight_date": args.flight_date,
    }
    with httpx.Client(timeout=60, trust_env=False) as client:
        response = client.post(
            f"{args.base_url.rstrip('/')}/v1/monitoring/poll", json=request
        )
        response.raise_for_status()
        outcome = response.json()

    # Provider credentials never appear in the canonical monitoring outcome.
    print(json.dumps(outcome, indent=2))
    return 0 if outcome.get("status") != "poll_failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
