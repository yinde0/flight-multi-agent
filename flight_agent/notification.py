from __future__ import annotations

import hashlib
import os
import re
import threading

from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlparse

import httpx

from flight_agent.notification_contracts import (
    NotificationCommand,
    NotificationReceipt,
)


class NotificationProvider(Protocol):
    def send(self, command: NotificationCommand) -> NotificationReceipt: ...


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


class RecordingNotificationProvider:
    """Non-delivering provider used to verify authorization and idempotency."""

    def __init__(self) -> None:
        self._receipts: dict[str, NotificationReceipt] = {}
        self._lock = threading.Lock()
        self._call_count = 0
        self._unique_delivery_count = 0

    def send(self, command: NotificationCommand) -> NotificationReceipt:
        with self._lock:
            self._call_count += 1
            existing = self._receipts.get(command.idempotency_key)
            if existing is not None:
                return existing.model_copy(update={"status": "duplicate"})
            digest = hashlib.sha256(
                command.idempotency_key.encode("utf-8")
            ).hexdigest()[:16]
            receipt = NotificationReceipt(
                notification_id=command.notification_id,
                decision_id=command.approval.decision_id,
                idempotency_key=command.idempotency_key,
                provider="recording",
                provider_delivery_id=f"recording-{digest}",
                status="delivered",
                delivered_at=_now_utc(),
            )
            self._receipts[command.idempotency_key] = receipt
            self._unique_delivery_count += 1
            return receipt

    def audit(self) -> dict[str, int]:
        with self._lock:
            return {
                "provider_call_count": self._call_count,
                "unique_delivery_count": self._unique_delivery_count,
            }


def render_sms_body(command: NotificationCommand) -> str:
    category = command.template_variables.get("category", "STATUS_CHANGE")
    messages = {
        "CANCELLATION": "Your flight has been cancelled.",
        "DIVERSION": "Your flight has been diverted.",
        "CONNECTION_RISK": "Your connection may now be at risk.",
        "DELAY": "A significant flight delay was detected.",
        "TERMINAL_CHANGE": "A significant terminal change was detected.",
        "WEATHER_RISK": "Severe weather may affect your flight.",
        "STATUS_CHANGE": "A significant flight-status change was detected.",
    }
    detail = messages.get(category, "A significant travel disruption was detected.")
    next_step = (
        " We are checking alternative flights."
        if command.search_requested
        else ""
    )
    return f"Travel Watch: {detail}{next_step} Open the app for details. Reply STOP to opt out."


class TwilioNotificationProvider:
    """Submit consented SMS messages to Twilio without logging recipient PII."""

    def __init__(
        self,
        *,
        account_sid: str,
        username: str,
        password: str,
        messaging_service_sid: str | None = None,
        from_number: str | None = None,
        status_callback_url: str | None = None,
        sms_body_override: str | None = None,
        base_url: str = "https://api.twilio.com/2010-04-01",
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if re.fullmatch(r"AC[0-9a-fA-F]{32}", account_sid) is None:
            raise RuntimeError("TWILIO_ACCOUNT_SID is missing or invalid")
        if not username or not password:
            raise RuntimeError("Twilio API credentials are incomplete")
        if messaging_service_sid is not None and re.fullmatch(
            r"MG[0-9a-fA-F]{32}", messaging_service_sid
        ) is None:
            raise RuntimeError("TWILIO_MESSAGING_SERVICE_SID is invalid")
        if not messaging_service_sid and not from_number:
            raise RuntimeError(
                "Twilio requires TWILIO_MESSAGING_SERVICE_SID or TWILIO_FROM_NUMBER"
            )
        if status_callback_url:
            parsed_callback = urlparse(status_callback_url)
            if (
                parsed_callback.scheme not in {"http", "https"}
                or not parsed_callback.netloc
                or "_" in (parsed_callback.hostname or "")
            ):
                raise RuntimeError("TWILIO_STATUS_CALLBACK_URL is invalid")
        self._account_sid = account_sid
        self._auth = (username, password)
        self._messaging_service_sid = messaging_service_sid
        self._from_number = from_number
        self._status_callback_url = status_callback_url
        self._sms_body_override = sms_body_override
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            trust_env=False,
        )
        self._receipts: dict[str, NotificationReceipt] = {}
        self._lock = threading.Lock()
        self._call_count = 0
        self._unique_delivery_count = 0

    @classmethod
    def from_environment(cls) -> "TwilioNotificationProvider":
        account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        api_key = os.getenv("TWILIO_API_KEY", "")
        api_secret = os.getenv("TWILIO_API_SECRET", "")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN", "") or os.getenv(
            "TWILIO_AUTH_KEY", ""
        )
        if api_key and api_secret:
            username, password = api_key, api_secret
        elif account_sid and auth_token:
            username, password = account_sid, auth_token
        else:
            raise RuntimeError(
                "Twilio needs TWILIO_API_KEY and TWILIO_API_SECRET, or TWILIO_AUTH_TOKEN"
            )
        return cls(
            account_sid=account_sid,
            username=username,
            password=password,
            messaging_service_sid=os.getenv("TWILIO_MESSAGING_SERVICE_SID") or None,
            from_number=os.getenv("TWILIO_FROM_NUMBER") or None,
            status_callback_url=os.getenv("TWILIO_STATUS_CALLBACK_URL") or None,
            sms_body_override=os.getenv("TWILIO_SMS_BODY_OVERRIDE") or None,
            base_url=os.getenv(
                "TWILIO_BASE_URL", "https://api.twilio.com/2010-04-01"
            ),
            timeout_seconds=float(os.getenv("TWILIO_TIMEOUT_SECONDS", "30")),
        )

    def send(self, command: NotificationCommand) -> NotificationReceipt:
        if command.channel != "sms" or command.recipient_address is None:
            raise RuntimeError("Twilio provider accepts consented SMS commands only")
        with self._lock:
            self._call_count += 1
            existing = self._receipts.get(command.idempotency_key)
            if existing is not None:
                return existing.model_copy(update={"status": "duplicate"})
            data = {
                "To": command.recipient_address,
                "Body": self._sms_body_override or render_sms_body(command),
            }
            if self._messaging_service_sid:
                data["MessagingServiceSid"] = self._messaging_service_sid
            else:
                data["From"] = str(self._from_number)
            if self._status_callback_url:
                data["StatusCallback"] = self._status_callback_url
            try:
                response = self._client.post(
                    f"{self._base_url}/Accounts/{self._account_sid}/Messages.json",
                    data=data,
                    auth=self._auth,
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise RuntimeError("Twilio message submission failed") from error
            provider_id = payload.get("sid") if isinstance(payload, dict) else None
            if not isinstance(provider_id, str) or re.fullmatch(
                r"(?:SM|MM)[0-9a-fA-F]{32}", provider_id
            ) is None:
                raise RuntimeError("Twilio returned an invalid message identifier")
            provider_status = str(payload.get("status") or "accepted")
            receipt = NotificationReceipt(
                notification_id=command.notification_id,
                decision_id=command.approval.decision_id,
                idempotency_key=command.idempotency_key,
                provider="twilio",
                provider_delivery_id=provider_id,
                status="accepted",
                delivered_at=_now_utc(),
                provider_status=provider_status,
            )
            self._receipts[command.idempotency_key] = receipt
            self._unique_delivery_count += 1
            return receipt

    def audit(self) -> dict[str, int]:
        with self._lock:
            return {
                "provider_call_count": self._call_count,
                "unique_delivery_count": self._unique_delivery_count,
            }
