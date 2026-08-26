from __future__ import annotations

import hashlib
import threading

from datetime import datetime, timezone
from typing import Protocol

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

    def send(self, command: NotificationCommand) -> NotificationReceipt:
        with self._lock:
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
            return receipt
