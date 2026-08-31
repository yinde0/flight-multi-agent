from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from flight_agent.notification import TwilioNotificationProvider, render_sms_body
from flight_agent.notification_contracts import EvalApproval, NotificationCommand
from flight_agent.notification_errors import NotificationSubmissionError


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


@pytest.mark.parametrize("http_status,code,retryable,remediation", [
    (400, 21608, False, "verify_recipient"),
    (401, 20003, False, "check_credentials"),
    (400, 21606, False, "check_sender"),
    (429, 20429, True, "retry_later"),
    (503, 20503, True, "retry_later"),
])
def test_twilio_preserves_safe_codes_and_retry_classification(http_status, code, retryable, remediation):
    def handler(request):
        return httpx.Response(http_status, request=request, json={
            "code": code, "message": "private provider detail +447700900123 api_key=SECRET",
            "more_info": "https://private.example/SECRET",
        })
    provider = TwilioNotificationProvider(
        account_sid=ACCOUNT_SID, username=API_KEY, password="test-api-secret",
        from_number="+447700900100", client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(NotificationSubmissionError) as caught:
        provider.send(sms_command())
    failure = caught.value.failure
    assert failure.error_code == f"TWILIO_{code}"
    assert failure.retryable is retryable
    assert failure.remediation == remediation
    assert "SECRET" not in failure.model_dump_json()
    assert "+447700" not in str(caught.value)
    assert provider.audit()["unique_delivery_count"] == 0


def test_trial_body_rejection_has_actionable_safe_guidance():
    from flight_agent.notification_errors import rejected_submission

    failure = rejected_submission(http_status=400, payload={
        "code": "not-a-number-SECRET",
        "message": "Trial account cannot send custom message body to +447700900123",
    })
    assert failure.error_code == "TWILIO_HTTP_400"
    assert failure.retryable is False
    assert failure.remediation == "upgrade_or_use_trial_template"
    assert "SECRET" not in failure.model_dump_json()
    assert "+447700" not in failure.model_dump_json()


def test_uncertain_post_timeout_is_not_automatically_retried():
    def handler(request):
        raise httpx.ReadTimeout("private network detail", request=request)
    provider = TwilioNotificationProvider(
        account_sid=ACCOUNT_SID, username=API_KEY, password="test-api-secret",
        from_number="+447700900100", client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(NotificationSubmissionError) as caught:
        provider.send(sms_command())
    assert caught.value.failure.error_code == "TWILIO_SUBMISSION_UNCERTAIN"
    assert caught.value.failure.retryable is False
    assert caught.value.failure.remediation == "check_delivery_before_retry"


def test_failure_contract_survives_real_fastmcp_output_and_client(monkeypatch):
    import asyncio
    from flight_agent import notification_mcp
    from flight_agent.notification_contracts import NotificationSubmissionFailure
    from flight_agent.notification_mcp_client import StreamableHttpNotificationMcpClient

    failure = NotificationSubmissionFailure(error_code="TWILIO_HTTP_400", retryable=False,
        http_status=400, remediation="upgrade_or_use_trial_template")

    class FailingProvider:
        def send(self, command):
            raise NotificationSubmissionError(failure)

    monkeypatch.setattr(notification_mcp, "provider", FailingProvider())
    monkeypatch.setattr(notification_mcp.failure_gate, "enabled", lambda: False)
    _content, structured = asyncio.run(notification_mcp.mcp.call_tool(
        "send_notification", {"command": sms_command().model_dump(mode="json")},
    ))
    client = StreamableHttpNotificationMcpClient("http://never-called/mcp")

    async def call_tool(*_args):
        return structured

    monkeypatch.setattr(client, "_call_tool", call_tool)
    with pytest.raises(NotificationSubmissionError) as caught:
        client.send_notification(sms_command())
    assert caught.value.failure == failure


def test_receipt_survives_real_fastmcp_union_output_and_client(monkeypatch):
    import asyncio
    from flight_agent import notification_mcp
    from flight_agent.notification import RecordingNotificationProvider
    from flight_agent.notification_mcp_client import StreamableHttpNotificationMcpClient

    monkeypatch.setattr(notification_mcp, "provider", RecordingNotificationProvider())
    monkeypatch.setattr(notification_mcp.failure_gate, "enabled", lambda: False)
    command = sms_command()
    _content, structured = asyncio.run(notification_mcp.mcp.call_tool(
        "send_notification", {"command": command.model_dump(mode="json")},
    ))
    client = StreamableHttpNotificationMcpClient("http://never-called/mcp")

    async def call_tool(*_args):
        return structured

    monkeypatch.setattr(client, "_call_tool", call_tool)
    receipt = client.send_notification(command)
    assert receipt.notification_id == command.notification_id
    assert receipt.provider == "recording"
    assert receipt.status == "delivered"


@pytest.mark.parametrize("code", [True, "²", -1, 1000000, {"private": "SECRET"}])
def test_malformed_provider_code_uses_safe_http_fallback(code):
    from flight_agent.notification_errors import rejected_submission

    failure = rejected_submission(http_status=400, payload={"code": code})
    assert failure.error_code == "TWILIO_HTTP_400"
    assert failure.retryable is False
