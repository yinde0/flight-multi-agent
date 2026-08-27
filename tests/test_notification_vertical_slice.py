from __future__ import annotations

import copy

from datetime import datetime, timezone
from typing import Any

import pytest

from pydantic import ValidationError

from flight_agent.notification import RecordingNotificationProvider
from flight_agent.notification_action_service import process_confirmed_event
from flight_agent.notification_contracts import (
    ConfirmedDisruptionEvent,
    EvalApproval,
    NotificationCommand,
)


class NotificationMemoryStore:
    def __init__(self) -> None:
        self.decisions: dict[str, dict[str, Any]] = {}
        self.confirmed: dict[str, dict[str, Any]] = {}
        self.notifications: dict[str, dict[str, Any]] = {}

    def get_decision(self, candidate_id):
        return copy.deepcopy(self.decisions.get(candidate_id))

    def get_confirmed_event(self, candidate_id):
        return copy.deepcopy(self.confirmed.get(candidate_id))

    def get_notification(self, decision_id):
        return copy.deepcopy(self.notifications.get(decision_id))

    def put_notification(self, decision_id, notification):
        self.notifications[decision_id] = copy.deepcopy(notification)


class RecordingGateway:
    def __init__(self) -> None:
        self.provider = RecordingNotificationProvider()
        self.calls: list[NotificationCommand] = []

    def send_notification(self, command: NotificationCommand):
        self.calls.append(command)
        return self.provider.send(command)


class FailingGateway:
    def send_notification(self, command: NotificationCommand):
        del command
        raise RuntimeError("simulated MCP outage")


def confirmed_event() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "event_type": "disruption_confirmed",
        "candidate_id": "cand-v5-001",
        "decision_id": "decision-v5-001",
        "trip_id": "trip-v5",
        "leg_id": "leg-v5",
        "category": "DELAY",
        "verdict": "NOTIFY",
        "reason_codes": ["DELAY_NOTIFY_THRESHOLD"],
        "published_at": "2026-09-15T06:15:00Z",
    }


def authorized_store() -> NotificationMemoryStore:
    store = NotificationMemoryStore()
    event = confirmed_event()
    store.confirmed[event["candidate_id"]] = copy.deepcopy(event)
    store.decisions[event["candidate_id"]] = {
        "schema_version": "1.0.0",
        "candidate_id": event["candidate_id"],
        "decision_id": event["decision_id"],
        "trip_id": event["trip_id"],
        "leg_id": event["leg_id"],
        "verdict": event["verdict"],
        "reason_codes": event["reason_codes"],
        "policy_version": "1.2.0",
        "decided_at": event["published_at"],
    }
    return store


def test_verified_confirmed_event_reaches_notification_provider_once() -> None:
    store = authorized_store()
    gateway = RecordingGateway()

    first = process_confirmed_event(
        confirmed_event(), store=store, notifier=gateway, authority_timeout_seconds=0
    )
    second = process_confirmed_event(
        confirmed_event(), store=store, notifier=gateway, authority_timeout_seconds=0
    )

    assert first.status == "delivered"
    assert second == first
    assert first.provider == "recording"
    assert len(gateway.calls) == 1
    assert gateway.calls[0].recipient_ref == "traveler:trip-v5"
    assert store.notifications[first.decision_id]["status"] == "delivered"


def test_forged_event_is_rejected_before_notification_mcp() -> None:
    store = authorized_store()
    gateway = RecordingGateway()
    forged = confirmed_event()
    forged["reason_codes"] = ["FLIGHT_CANCELLED"]

    result = process_confirmed_event(
        forged, store=store, notifier=gateway, authority_timeout_seconds=0
    )

    assert result.status == "rejected"
    assert result.error_code == "EVAL_AUTHORITY_MISMATCH"
    assert gateway.calls == []
    assert store.notifications == {}


def test_notification_mcp_failure_is_audited_without_fake_delivery() -> None:
    store = authorized_store()
    result = process_confirmed_event(
        confirmed_event(),
        store=store,
        notifier=FailingGateway(),
        authority_timeout_seconds=0,
    )

    assert result.status == "failed"
    assert result.error_code == "NOTIFICATION_MCP_FAILED"
    assert result.provider is None
    assert store.notifications[result.decision_id]["status"] == "failed"


def test_suppressed_verdict_cannot_cross_notification_contract() -> None:
    event = confirmed_event()
    event["verdict"] = "SUPPRESS"
    with pytest.raises(ValidationError):
        ConfirmedDisruptionEvent.model_validate(event)

    with pytest.raises(ValidationError):
        NotificationCommand(
            notification_id="notification-v5",
            idempotency_key="notification:decision-v5",
            trip_id="trip-v5",
            leg_id="leg-v5",
            recipient_ref="traveler:trip-v5",
            template_variables={"category": "DELAY"},
            search_requested=False,
            approval=EvalApproval.model_validate(
                {
                    "candidate_id": "cand-v5",
                    "decision_id": "decision-v5",
                    "verdict": "SUPPRESS",
                    "policy_version": "1.2.0",
                    "reason_codes": ["DELAY_BELOW_NOTIFY_THRESHOLD"],
                    "decided_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
        )
