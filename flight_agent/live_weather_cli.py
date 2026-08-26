from __future__ import annotations

import argparse
import json
import os

from datetime import datetime, timedelta, timezone

from flight_agent.weather_mcp_client import StreamableHttpWeatherMcpClient


def _default_target() -> str:
    target = datetime.now(timezone.utc) + timedelta(hours=6)
    return target.isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read one live airport forecast through the internal weather MCP."
    )
    parser.add_argument("airport", nargs="?", default="LHR")
    parser.add_argument("target_at", nargs="?", default=_default_target())
    args = parser.parse_args()
    client = StreamableHttpWeatherMcpClient(
        os.getenv("WEATHER_MCP_URL", "http://weather-mcp:8006/mcp")
    )
    observation = client.get_airport_weather(
        airport=args.airport.upper(), target_at=args.target_at
    )
    print(json.dumps(observation.model_dump(mode="json"), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
