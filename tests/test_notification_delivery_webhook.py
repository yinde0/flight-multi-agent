from __future__ import annotations

import copy

from typing import Any

from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from flight_agent.notification_contracts import TwilioSmsStatusCallback
from flight_agent.notification_webhook import (
    create_notification_webhook_app,
    reconcile_twilio_sms_status,
)


ACCOUNT_SID = "AC" + "a" * 32
MESSAGE_SID = "SM" + "d" * 32
AUTH_TOKEN = "synthetic-primary-auth-token"
CALLBACK_URL = "https://travel.test/v1/webhooks/twilio/status"


def notification_record() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "notification_id": "notification-v14-001",
        "candidate_id": "candidate-v14-001",
        "decision_id": "decision-v14-001",
        "trip_id": "trip-v14-001",
        "leg_id": "leg-v14-001",
        "verdict": "NOTIFY",
        "status": "accepted",
        "idempotency_key": "notification:decision-v14-001",
        "provider": "twilio",
        "provider_delivery_id": MESSAGE_SID,
        "provider_status": "queued",
        "recorded_at": "2026-09-15T06:10:00Z",
        "delivery_updated_at": None,
        "error_code": None,
    }


class DeliveryMemoryStore:
    def __init__(self) -> None:
        self.records = {"decision-v14-001": notification_record()}

    def get_notification_by_provider_delivery(self, provider, provider_delivery_id):
        if provider != "twilio" or provider_delivery_id != MESSAGE_SID:
            return None
        return "decision-v14-001", copy.deepcopy(
            self.records["decision-v14-001"]
        )

    def compare_and_set_notification(
        self, decision_id, *, expected, replacement
    ):
        if self.records.get(decision_id) != expected:
            return False
        self.records[decision_id] = copy.deepcopy(replacement)
        return True


def callback_params(status: str, *, error_code: int | None = None):
    params = {
        "AccountSid": ACCOUNT_SID,
        "MessageSid": MESSAGE_SID,
        "MessageStatus": status,
        "FutureTwilioField": "accepted-for-signature-evolution",
    }
    if error_code is not None:
        params["ErrorCode"] = str(error_code)
    return params


def signed_headers(params: dict[str, str]) -> dict[str, str]:
    signature = RequestValidator(AUTH_TOKEN).compute_signature(
        CALLBACK_URL, params
    )
    return {"X-Twilio-Signature": signature}


def test_signed_callbacks_advance_once_and_never_regress_terminal_state() -> None:
    store = DeliveryMemoryStore()
    app = create_notification_webhook_app(
        store=store,
        enabled=True,
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        public_callback_url=CALLBACK_URL,
    )

    with TestClient(app) as client:
        sent = callback_params("sent")
        assert client.post(
            "/v1/webhooks/twilio/status",
            data=sent,
            headers=signed_headers(sent),
        ).status_code == 204
        assert store.records["decision-v14-001"]["status"] == "accepted"
        assert store.records["decision-v14-001"]["provider_status"] == "sent"

        delivered = callback_params("delivered")
        assert client.post(
            "/v1/webhooks/twilio/status",
            data=delivered,
            headers=signed_headers(delivered),
        ).status_code == 204
        terminal = copy.deepcopy(store.records["decision-v14-001"])
        assert terminal["status"] == "delivered"

        assert client.post(
            "/v1/webhooks/twilio/status",
            data=delivered,
            headers=signed_headers(delivered),
        ).status_code == 204
        stale = callback_params("sent")
        assert client.post(
            "/v1/webhooks/twilio/status",
            data=stale,
            headers=signed_headers(stale),
        ).status_code == 204
        assert store.records["decision-v14-001"] == terminal


def test_forged_callback_is_rejected_without_mutating_delivery() -> None:
    store = DeliveryMemoryStore()
    app = create_notification_webhook_app(
        store=store,
        enabled=True,
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        public_callback_url=CALLBACK_URL,
    )
    before = copy.deepcopy(store.records)

    with TestClient(app) as client:
        response = client.post(
            "/v1/webhooks/twilio/status",
            data=callback_params("delivered"),
            headers={"X-Twilio-Signature": "forged"},
        )

    assert response.status_code == 403
    assert store.records == before


def test_failed_callback_preserves_provider_error_without_recipient_pii() -> None:
    store = DeliveryMemoryStore()
    outcome = reconcile_twilio_sms_status(
        TwilioSmsStatusCallback(
            account_sid=ACCOUNT_SID,
            message_sid=MESSAGE_SID,
            message_status="undelivered",
            error_code=30003,
        ),
        store=store,
        updated_at="2026-09-15T06:12:00Z",
    )

    record = store.records["decision-v14-001"]
    assert outcome.applied is True
    assert outcome.action_status == "failed"
    assert record["status"] == "failed"
    assert record["provider_status"] == "undelivered"
    assert record["error_code"] == "TWILIO_30003"
    assert "+447" not in str(record)


def test_unknown_delivery_is_acknowledged_without_creating_a_record() -> None:
    store = DeliveryMemoryStore()
    unknown = TwilioSmsStatusCallback(
        account_sid=ACCOUNT_SID,
        message_sid="SM" + "e" * 32,
        message_status="delivered",
    )

    outcome = reconcile_twilio_sms_status(unknown, store=store)

    assert outcome.found is False
    assert outcome.applied is False
    assert outcome.ignored_reason == "UNKNOWN_DELIVERY"
