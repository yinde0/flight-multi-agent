from __future__ import annotations

import copy

from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from pydantic import ValidationError

from flight_agent.notification import (
    RecordingNotificationProvider,
    TwilioNotificationProvider,
)
from flight_agent.disruption_explanation import DisruptionExplanation
from flight_agent.notification_action_service import process_confirmed_event
from flight_agent.notification_contracts import (
    ConfirmedDisruptionEvent,
    EvalApproval,
    NotificationCommand,
)
from flight_agent.trip_contracts import NotificationRecipient


class NotificationMemoryStore:
    def __init__(self) -> None:
        self.decisions: dict[str, dict[str, Any]] = {}
        self.confirmed: dict[str, dict[str, Any]] = {}
        self.notifications: dict[str, dict[str, Any]] = {}
        self.candidates: dict[str, dict[str, Any]] = {}

    def get_candidate(self, candidate_id):
        return copy.deepcopy(self.candidates.get(candidate_id))

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


def test_provider_rejection_is_persisted_with_actionable_safe_details():
    from flight_agent.notification_contracts import NotificationSubmissionFailure
    from flight_agent.notification_errors import NotificationSubmissionError

    class RejectedGateway:
        def send_notification(self, command):
            raise NotificationSubmissionError(NotificationSubmissionFailure(
                error_code="TWILIO_HTTP_400", retryable=False, http_status=400,
                remediation="upgrade_or_use_trial_template",
            ))

    store = authorized_store()
    record = process_confirmed_event(
        confirmed_event(), store=store, notifier=RejectedGateway(),
        communicator=StaticCommunicationAgent(), authority_timeout_seconds=0,
    )
    assert record.status == "failed"
    assert record.provider == "twilio"
    assert record.error_code == "TWILIO_HTTP_400"
    assert record.submission_failure.retryable is False
    assert store.get_notification(record.decision_id)["submission_failure"]["remediation"] == "upgrade_or_use_trial_template"


@pytest.mark.parametrize("retryable", [False, True])
def test_notification_consumer_quarantines_permanent_rejections_on_first_attempt(monkeypatch, retryable):
    import asyncio
    import json
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from flight_agent import notification_action_service as service
    from flight_agent.event_delivery import DurableEventEnvelope
    from flight_agent.notification_contracts import NotificationSubmissionFailure
    from flight_agent.notification_errors import NotificationSubmissionError

    class RejectedGateway:
        def send_notification(self, command):
            raise NotificationSubmissionError(NotificationSubmissionFailure(
                error_code="TWILIO_HTTP_429" if retryable else "TWILIO_HTTP_400",
                retryable=retryable,
                remediation="retry_later" if retryable else "upgrade_or_use_trial_template",
            ))

    async def no_op(*_args, **_kwargs):
        pass

    callback = {}
    async def subscribe(*_args, **kwargs):
        callback["handle"] = kwargs["callback"]
        return SimpleNamespace(unsubscribe=no_op)

    async def connect(*_args):
        return SimpleNamespace(jetstream=lambda: object(), drain=no_op)

    monkeypatch.setattr(service, "connect_nats", connect)
    monkeypatch.setattr(service, "ensure_event_stream", no_op)
    monkeypatch.setattr(service, "subscribe_durable", subscribe)
    store = authorized_store()
    quarantined = []
    store.put_dead_letter = lambda **record: quarantined.append(record)
    event = confirmed_event()

    class Message:
        data = DurableEventEnvelope(
            event_id=event["decision_id"], event_type="disruption_confirmed",
            occurred_at=event["published_at"], payload=event,
        ).model_dump_json().encode()
        metadata = SimpleNamespace(num_delivered=1)
        headers = None
        terminated = False
        delays = []

        async def term(self):
            self.terminated = True

        async def nak(self, delay=None):
            self.delays.append(delay)

    app = service.create_notification_action_app(
        store=store, notifier=RejectedGateway(), communicator=StaticCommunicationAgent(),
        delivery_provider="recording",
    )
    message = Message()
    with TestClient(app):
        asyncio.run(callback["handle"](message))
    assert message.terminated is not retryable
    assert bool(message.delays) is retryable
    assert bool(quarantined) is not retryable
    if quarantined:
        assert quarantined[0]["attempts"] == 1
        assert quarantined[0]["error_code"] == "TWILIO_HTTP_400"
        assert "recipient_address" not in json.dumps(quarantined)


class StaticRecipientResolver:
    def get_recipient(self, trip_id: str) -> NotificationRecipient | None:
        return NotificationRecipient(
            trip_id=trip_id,
            recipient_ref=f"traveler:{trip_id}",
            phone_e164="+447700900123",
            consent_granted_at="2026-09-15T05:00:00Z",
        )


class StaticCommunicationAgent:
    def __init__(self) -> None:
        self.calls = []

    def explain(self, request):
        self.calls.append(request)
        return DisruptionExplanation(
            message=(
                "Your flight is now delayed by 45 minutes. "
                "We'll keep watching for further changes."
            ),
            status="generated",
            source="azure_openai",
            model="fixture-gpt-deployment",
            confidence=0.98,
        )


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
    store.candidates[event["candidate_id"]] = {
        "candidate_id": event["candidate_id"],
        "category": "DELAY",
        "delay_minutes": 45,
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


def test_friendly_explanation_is_added_only_after_eval_approval() -> None:
    store = authorized_store()
    gateway = RecordingGateway()
    communicator = StaticCommunicationAgent()

    result = process_confirmed_event(
        confirmed_event(),
        store=store,
        notifier=gateway,
        communicator=communicator,
        authority_timeout_seconds=0,
    )
    replay = process_confirmed_event(
        confirmed_event(),
        store=store,
        notifier=gateway,
        communicator=communicator,
        authority_timeout_seconds=0,
    )

    assert result.explanation_status == "generated"
    assert result.explanation_source == "azure_openai"
    assert result.friendly_message and "45 minutes" in result.friendly_message
    assert gateway.calls[0].template_variables["friendly_message"] == (
        result.friendly_message
    )
    assert len(communicator.calls) == 1
    assert communicator.calls[0].delay_minutes == 45
    assert replay == result
    assert len(gateway.calls) == 1


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


@pytest.mark.parametrize("body_override", [None, ""])
def test_approved_llm_wording_reaches_twilio_body_once_without_override(
    body_override: str | None,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            201, request=request, json={"sid": "SM" + "d" * 32, "status": "queued"}
        )

    store = authorized_store()
    communicator = StaticCommunicationAgent()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = TwilioNotificationProvider(
            account_sid="AC" + "a" * 32,
            username="SK" + "b" * 32,
            password="synthetic-secret",
            from_number="+447700900100",
            sms_body_override=body_override,
            base_url="https://twilio.test/2010-04-01",
            client=client,
        )

        class TwilioGateway:
            def send_notification(self, command: NotificationCommand):
                return provider.send(command)

        gateway = TwilioGateway()
        kwargs = {
            "store": store,
            "notifier": gateway,
            "communicator": communicator,
            "recipient_resolver": StaticRecipientResolver(),
            "delivery_provider": "twilio",
            "authority_timeout_seconds": 0,
        }
        first = process_confirmed_event(confirmed_event(), **kwargs)
        replay = process_confirmed_event(confirmed_event(), **kwargs)

    assert first.status == "accepted"
    assert first.explanation_source == "azure_openai"
    assert replay == first
    assert len(communicator.calls) == 1
    assert len(captured) == 1
    body = parse_qs(captured[0].content.decode("utf-8"))["Body"][0]
    assert body == (
        f"Travel Watch: {first.friendly_message} "
        "Open the app for details. Reply STOP to opt out."
    )
    assert "45 minutes" in body
    assert "sms_appointment_reminders" not in body
    assert "checking alternative flights" not in body
    assert "trip-v5" not in body
    assert "+447700900123" not in body


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


def test_twilio_mode_resolves_consented_phone_only_after_eval_approval() -> None:
    store = authorized_store()
    gateway = RecordingGateway()

    result = process_confirmed_event(
        confirmed_event(),
        store=store,
        notifier=gateway,
        recipient_resolver=StaticRecipientResolver(),
        delivery_provider="twilio",
        authority_timeout_seconds=0,
    )

    assert result.status == "delivered"
    assert len(gateway.calls) == 1
    assert gateway.calls[0].channel == "sms"
    assert gateway.calls[0].recipient_address == "+447700900123"


def test_twilio_mode_rejects_notification_when_consent_is_unavailable() -> None:
    store = authorized_store()
    gateway = RecordingGateway()

    result = process_confirmed_event(
        confirmed_event(),
        store=store,
        notifier=gateway,
        delivery_provider="twilio",
        authority_timeout_seconds=0,
    )

    assert result.status == "rejected"
    assert result.error_code == "SMS_RECIPIENT_UNAVAILABLE"
    assert gateway.calls == []


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
