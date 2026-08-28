from __future__ import annotations

from typing import Protocol

import httpx

from flight_agent.trip_contracts import NotificationRecipient


class NotificationRecipientResolver(Protocol):
    def get_recipient(self, trip_id: str) -> NotificationRecipient | None: ...


class HttpNotificationRecipientResolver:
    """Resolve a consented recipient over the private orchestrator network."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def get_recipient(self, trip_id: str) -> NotificationRecipient | None:
        with httpx.Client(timeout=self._timeout_seconds, trust_env=False) as client:
            response = client.get(
                f"{self._base_url}/v1/trips/{trip_id}/notification-recipient"
            )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return NotificationRecipient.model_validate(response.json())
