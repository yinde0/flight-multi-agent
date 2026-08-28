from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from flight_agent.notification import TwilioNotificationProvider, render_sms_body
from flight_agent.notification_contracts import EvalApproval, NotificationCommand


ACCOUNT_SID = "AC" + "a" * 32
API_KEY = "SK" + "b" * 32
MESSAGING_SERVICE_SID = "MG" + "c" * 32
MESSAGE_SID = "SM" + "d" * 32
STATUS_CALLBACK_URL = "https://travel.test/v1/webhooks/twilio/status"


def sms_command() -> NotificationCommand:
    return NotificationCommand(
        notification_id="notification-twilio-001",
        idempotency_key="notification:decision-twilio-001",
        trip_id="trip-twilio-001",
        leg_id="leg-twilio-001",
        recipient_ref="traveler:trip-twilio-001",
        recipient_address="+447700900123",
        channel="sms",
        template_variables={"category": "CANCELLATION"},
        search_requested=True,
        approval=EvalApproval(
            candidate_id="candidate-twilio-001",
            decision_id="decision-twilio-001",
            verdict="NOTIFY_AND_SEARCH",
            policy_version="1.2.0",
            reason_codes=["FLIGHT_CANCELLED"],
            decided_at="2026-09-15T06:10:00Z",
        ),
    )


def test_twilio_provider_submits_one_idempotent_sms() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            request=request,
            json={"sid": MESSAGE_SID, "status": "queued"},
        )

    provider = TwilioNotificationProvider(
        account_sid=ACCOUNT_SID,
        username=API_KEY,
        password="test-api-secret",
        messaging_service_sid=MESSAGING_SERVICE_SID,
        status_callback_url=STATUS_CALLBACK_URL,
        base_url="https://twilio.test/2010-04-01",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    first = provider.send(sms_command())
    second = provider.send(sms_command())

    assert first.status == "accepted"
    assert first.provider == "twilio"
    assert first.provider_status == "queued"
    assert second.status == "duplicate"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == f"/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json"
    assert request.headers["authorization"].startswith("Basic ")
    form = parse_qs(request.content.decode("utf-8"))
    assert form["To"] == ["+447700900123"]
    assert form["MessagingServiceSid"] == [MESSAGING_SERVICE_SID]
    assert form["StatusCallback"] == [STATUS_CALLBACK_URL]
    assert "cancelled" in form["Body"][0]
    assert "STOP" in form["Body"][0]


def test_twilio_provider_requires_complete_credentials_and_sender() -> None:
    with pytest.raises(RuntimeError, match="credentials"):
        TwilioNotificationProvider(
            account_sid=ACCOUNT_SID,
            username="",
            password="",
            messaging_service_sid=MESSAGING_SERVICE_SID,
        )
    with pytest.raises(RuntimeError, match="requires"):
        TwilioNotificationProvider(
            account_sid=ACCOUNT_SID,
            username=API_KEY,
            password="secret",
        )


def test_sms_body_contains_no_internal_trip_or_leg_identifiers() -> None:
    body = render_sms_body(sms_command())

    assert "trip-twilio-001" not in body
    assert "leg-twilio-001" not in body


def test_sms_body_prefers_validated_friendly_explanation() -> None:
    command = sms_command().model_copy(
        update={
            "template_variables": {
                "category": "CANCELLATION",
                "friendly_message": "We’re sorry—your flight has been cancelled.",
            }
        }
    )

    body = render_sms_body(command)

    assert "We’re sorry—your flight has been cancelled." in body
    assert "checking alternative flights" in body
    assert "Reply STOP" in body


def test_twilio_provider_can_use_explicit_trial_body_without_changing_default() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            201,
            request=request,
            json={"sid": MESSAGE_SID, "status": "queued"},
        )

    provider = TwilioNotificationProvider(
        account_sid=ACCOUNT_SID,
        username=API_KEY,
        password="test-api-secret",
        from_number="+447700900100",
        sms_body_override="sms_appointment_reminders",
        base_url="https://twilio.test/2010-04-01",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    provider.send(sms_command())

    form = parse_qs(captured[0].content.decode("utf-8"))
    assert form["Body"] == ["sms_appointment_reminders"]
