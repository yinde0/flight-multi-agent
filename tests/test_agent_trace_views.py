from __future__ import annotations

import hashlib
import json

from flight_agent.contracts import DocumentMetadata
from flight_agent.eval_service import (
    eval_agent_trace_input,
    eval_agent_trace_output,
)
from flight_agent.flight_search_action_service import (
    search_agent_trace_input,
    search_agent_trace_output,
)
from flight_agent.flight_search_contracts import FlightSearchActionRecord
from flight_agent.flow import (
    document_agent_trace_input,
    document_agent_trace_output,
)
from flight_agent.monitoring_contracts import MonitoringPollRequest
from flight_agent.monitoring_flow import (
    monitoring_agent_trace_input,
    monitoring_agent_trace_output,
)
from flight_agent.notification_action_service import (
    notification_agent_trace_input,
    notification_agent_trace_output,
)
from flight_agent.notification_contracts import NotificationActionRecord
from travel_eval.policy import SuppressionPolicy


TRIP_ID = "trip-private-reference"
LEG_ID = "leg-private-reference"
CANDIDATE_ID = "cand-private-reference"
DECISION_ID = "decision-private-reference"


def test_document_agent_trace_view_is_useful_and_redacted() -> None:
    document = b"%PDF-synthetic"
    metadata = DocumentMetadata(
        trip_id=TRIP_ID,
        traveler_ref="traveler-private-reference",
        fixture_id="fixture-private-reference",
        filename="private-name.pdf",
        sha256=hashlib.sha256(document).hexdigest(),
    )
    trace_input = document_agent_trace_input(document, metadata)
    trace_output = document_agent_trace_output(
        {
            "status": "parsed",
            "itinerary": {
                "confirmation_codes": ["SECRET123"],
                "legs": [
                    {
                        "leg_id": LEG_ID,
                        "flight_number": "NB204",
                        "origin": "LHR",
                        "destination": "AMS",
                        "scheduled_departure_at": "2026-09-15T08:00:00Z",
                        "scheduled_arrival_at": "2026-09-15T09:15:00Z",
                    }
                ],
            },
            "orchestration": {"framework": "crewai-flow"},
        }
    )
    rendered = json.dumps({"input": trace_input, "output": trace_output})

    assert trace_output["itinerary"]["legs"][0]["flight_number"] == "NB204"
    assert trace_output["itinerary"]["confirmation_count"] == 1
    for secret in (
        TRIP_ID,
        LEG_ID,
        "traveler-private-reference",
        "fixture-private-reference",
        "private-name.pdf",
        "SECRET123",
    ):
        assert secret not in rendered


def test_monitor_and_eval_trace_views_show_candidate_and_decision() -> None:
    request = MonitoringPollRequest(
        trip_id=TRIP_ID,
        leg_id=LEG_ID,
        flight_iata="NB204",
        flight_date="2026-09-15",
        replay_key="private-replay-key",
    )
    monitor_input = monitoring_agent_trace_input(request)
    monitor_output = monitoring_agent_trace_output(
        {
            "status": "candidate_evaluated",
            "observation": {
                "source": "replay",
                "status": "active",
                "departure_airport": "LHR",
                "destination_airport": "AMS",
                "departure": {
                    "scheduled_at": "2026-09-15T08:00:00Z",
                    "estimated_at": "2026-09-15T08:45:00Z",
                    "gate": "A12",
                },
                "weather": {"risk_level": "moderate"},
                "confidence": 0.98,
            },
            "candidate": {
                "candidate_id": CANDIDATE_ID,
                "category": "DELAY",
                "delay_minutes": 45,
                "score": 0.75,
                "confidence": 0.98,
            },
            "decision": {
                "verdict": "NOTIFY",
                "reason_codes": ["DELAY_NOTIFY_THRESHOLD"],
                "policy_version": "v1",
            },
            "orchestration": {
                "notification_action": {"status": "pending"},
                "search_action": {"status": "not_required"},
            },
        }
    )
    candidate = {
        "candidate_id": CANDIDATE_ID,
        "trip_id": TRIP_ID,
        "leg_id": LEG_ID,
        "category": "DELAY",
        "delay_minutes": 45,
        "weather_risk_level": "moderate",
        "confidence": 0.98,
        "score": 0.75,
    }
    policy = SuppressionPolicy(
        {
            "policy_version": "v1",
            "thresholds": {
                "delay_minutes": {"notify": 30, "search": 90},
                "cooldown_minutes": 60,
            },
        }
    )
    eval_input = eval_agent_trace_input(candidate, None, policy)  # type: ignore[arg-type]
    eval_output = eval_agent_trace_output(
        (
            {
                "verdict": "NOTIFY",
                "reason_codes": ["DELAY_NOTIFY_THRESHOLD"],
                "policy_version": "v1",
                "confidence": 0.98,
            },
            {"event_type": "disruption_confirmed"},
            "private-episode-key",
            2,
            False,
        )
    )
    rendered = json.dumps(
        {
            "monitor_input": monitor_input,
            "monitor_output": monitor_output,
            "eval_input": eval_input,
            "eval_output": eval_output,
        }
    )

    assert monitor_output["candidate"]["delay_minutes"] == 45
    assert eval_output["verdict"] == "NOTIFY"
    for secret in (TRIP_ID, LEG_ID, CANDIDATE_ID, "private-replay-key"):
        assert secret not in rendered


def test_action_agent_trace_views_show_results_without_authority_ids() -> None:
    event = {
        "candidate_id": CANDIDATE_ID,
        "decision_id": DECISION_ID,
        "trip_id": TRIP_ID,
        "leg_id": LEG_ID,
        "category": "CANCELLATION",
        "verdict": "NOTIFY_AND_SEARCH",
        "reason_codes": ["FLIGHT_CANCELLED"],
    }
    notification = NotificationActionRecord(
        notification_id="notification-private-reference",
        candidate_id=CANDIDATE_ID,
        decision_id=DECISION_ID,
        trip_id=TRIP_ID,
        leg_id=LEG_ID,
        verdict="NOTIFY_AND_SEARCH",
        status="delivered",
        idempotency_key="notification:private-reference",
        provider="recording",
        provider_status="delivered",
        recorded_at="2026-09-15T08:45:00Z",
    )
    search = FlightSearchActionRecord(
        search_id="search-private-reference",
        candidate_id=CANDIDATE_ID,
        decision_id=DECISION_ID,
        trip_id=TRIP_ID,
        leg_id=LEG_ID,
        verdict="NOTIFY_AND_SEARCH",
        status="completed",
        idempotency_key="search:private-reference",
        provider="replay",
        source_scope="synthetic_replay",
        availability_verified=False,
        recorded_at="2026-09-15T08:45:00Z",
    )
    trace_value = {
        "notification_input": notification_agent_trace_input(
            event, delivery_provider="recording"
        ),
        "notification_output": notification_agent_trace_output(notification),
        "search_input": search_agent_trace_input(event),
        "search_output": search_agent_trace_output(search),
    }
    rendered = json.dumps(trace_value)

    assert trace_value["notification_output"]["status"] == "delivered"
    assert trace_value["search_output"]["alternative_count"] == 0
    for secret in (TRIP_ID, LEG_ID, CANDIDATE_ID, DECISION_ID):
        assert secret not in rendered
