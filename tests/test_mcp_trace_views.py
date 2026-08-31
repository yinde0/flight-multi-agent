from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from flight_agent import telemetry
from flight_agent.flight_search_contracts import FlightSearchCommand, FlightSearchToolResult
from flight_agent.flight_search_mcp_client import StreamableHttpFlightSearchMcpClient
from flight_agent.flight_status_mcp_client import StreamableHttpFlightStatusMcpClient
from flight_agent.notification_contracts import NotificationCommand, NotificationReceipt
from flight_agent.notification_mcp_client import StreamableHttpNotificationMcpClient
from flight_agent.weather_mcp_client import StreamableHttpWeatherMcpClient


FIXTURES = Path(__file__).resolve().parents[1] / "travel_eval" / "fixtures"
PRIVATE_URL = "http://private-server:9999/mcp?api_key=private-key"


def mcp_cases():
    flight = json.loads((FIXTURES / "monitoring/vertical_07_flight_timeline.json").read_text())["observations"][0]
    weather = json.loads((FIXTURES / "monitoring/vertical_07_weather_timeline.json").read_text())["observations"][0]
    approval = {
        "candidate_id": "private-candidate", "decision_id": "private-decision",
        "verdict": "NOTIFY_AND_SEARCH", "policy_version": "1.2.0",
        "reason_codes": ["FLIGHT_CANCELLED"], "decided_at": "2026-09-15T06:00:00Z",
    }
    notification = NotificationCommand(
        notification_id="private-notification", idempotency_key="private-idempotency",
        trip_id="private-trip", leg_id="private-leg", recipient_ref="traveler:private-ref",
        channel="sms", recipient_address="+447700900123", search_requested=True,
        template_variables={"friendly_message": "Your flight has been cancelled.", "trip_id": "private-trip", "unknown": "PRIVATE"},
        approval=approval,
    )
    receipt = NotificationReceipt(
        notification_id=notification.notification_id, decision_id="private-decision",
        idempotency_key="private-idempotency", provider="recording",
        provider_delivery_id="private-delivery", status="delivered", delivered_at="2026-09-15T06:00:00Z",
    )
    search = FlightSearchCommand(
        search_id="private-search", idempotency_key="private-idempotency",
        trip_id="private-trip", leg_id="private-leg", original_flight_iata="NB204",
        origin="LHR", destination="AMS", departure_date="2026-09-15",
        earliest_departure_at="2026-09-15T08:00:00Z", latest_departure_at="2026-09-15T20:00:00Z",
        approval=approval,
    )
    search_result = FlightSearchToolResult(
        search_id="private-search", decision_id="private-decision", idempotency_key="private-idempotency",
        provider="replay", source_scope="synthetic_replay", searched_at="2026-09-15T06:00:00Z",
        options=json.loads((FIXTURES / "search/vertical_07_options.json").read_text())["options"],
    )
    return [
        (StreamableHttpFlightStatusMcpClient(PRIVATE_URL), "get_flight_status", (),
         {"flight_iata": "NB204", "flight_date": "2026-09-15", "replay_key": "private-replay"}, flight, "status"),
        (StreamableHttpFlightStatusMcpClient(PRIVATE_URL), "discover_live_flight_sample", (), {},
         {"flight_iata": "NB204", "flight_date": "2026-09-15", "origin": "LHR", "destination": "AMS", "observation": flight}, "observation"),
        (StreamableHttpWeatherMcpClient(PRIVATE_URL), "get_airport_weather", (),
         {"airport": "LHR", "target_at": "2026-09-15T08:20:00Z", "replay_key": "private-replay"}, weather, "risk_level"),
        (StreamableHttpNotificationMcpClient(PRIVATE_URL), "send_notification", (notification,), {}, receipt.model_dump(mode="json"), "status"),
        (StreamableHttpFlightSearchMcpClient(PRIVATE_URL), "search_flights", (search,), {}, search_result.model_dump(mode="json"), "options"),
    ]


@pytest.mark.parametrize("client,method,args,kwargs,payload,output_key", mcp_cases(), ids=lambda value: value if isinstance(value, str) else None)
def test_mcp_boundaries_capture_safe_input_and_output(monkeypatch, client, method, args, kwargs, payload, output_key):
    attributes = {}
    names = []

    class Span:
        def set_attribute(self, key, value):
            attributes[key] = value

    @contextmanager
    def capture(operation, **_kwargs):
        names.append(operation)
        yield Span()

    async def fake_call(name, arguments):
        assert name == method
        # The real command is preserved at the MCP boundary, not redacted in transit.
        if method == "send_notification":
            assert arguments["command"]["recipient_address"] == "+447700900123"
        return payload

    monkeypatch.setattr(telemetry, "trace_operation", capture)
    monkeypatch.setattr(telemetry, "_content_capture_enabled", True)
    monkeypatch.setattr(client, "_call_tool", fake_call)
    result = getattr(client, method)(*args, **kwargs)
    assert result is not None
    assert names == [f"mcp.{method}"]
    assert attributes["travel.trace.has_input"] is True
    assert attributes["travel.trace.has_output"] is True
    assert output_key in json.loads(attributes["gen_ai.completion.0.content"])
    rendered = json.dumps(attributes)
    for secret in ("private-", "+447700900123", "PRIVATE", "obs-v7-", "weather-v7-", "option-v7-"):
        assert secret not in rendered
