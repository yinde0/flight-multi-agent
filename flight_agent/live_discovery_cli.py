from __future__ import annotations

import os

from flight_agent.flight_status_mcp_client import (
    StreamableHttpFlightStatusMcpClient,
)


def main() -> int:
    client = StreamableHttpFlightStatusMcpClient(
        os.getenv("FLIGHT_STATUS_MCP_URL", "http://flight-status-mcp:8003/mcp")
    )
    sample = client.discover_live_flight_sample(limit=25)
    print(sample.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
