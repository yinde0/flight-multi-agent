from __future__ import annotations

import asyncio
import os

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import ValidationError
from twilio.request_validator import RequestValidator

from flight_agent.monitoring_store import DynamoMonitoringStateStore, MonitoringStore
from flight_agent.notification_contracts import (
    DeliveryReconciliationOutcome,
    NotificationActionRecord,
    TwilioSmsStatusCallback,
)
from flight_agent.telemetry import install_telemetry_routes


_PROGRESS_RANK = {
    "accepted": 0,
    "scheduled": 0,
    "queued": 1,
    "sending": 2,
    "sent": 3,
}
_TERMINAL_PROVIDER_STATUSES = {
    "delivered",
    "undelivered",
    "failed",
    "canceled",
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _action_status(provider_status: str) -> str:
    if provider_status == "delivered":
        return "delivered"
    if provider_status in {"undelivered", "failed", "canceled"}:
        return "failed"
    return "accepted"


def _ignored_outcome(
    callback: TwilioSmsStatusCallback,
    *,
    reason: str,
    record: dict[str, Any] | None = None,
    decision_id: str | None = None,
    duplicate: bool = False,
) -> DeliveryReconciliationOutcome:
    return DeliveryReconciliationOutcome(
        provider_delivery_id=callback.message_sid,
        found=record is not None,
        applied=False,
        duplicate=duplicate,
        ignored_reason=reason,
        decision_id=decision_id,
        notification_id=(
            str(record["notification_id"])
            if record is not None and record.get("notification_id")
            else None
        ),
        previous_provider_status=(
            str(record["provider_status"])
            if record is not None and record.get("provider_status")
            else None
        ),
        provider_status=callback.message_status,
        action_status=(
            record.get("status")
            if record is not None
            and record.get("status") in {"accepted", "delivered", "failed"}
            else None
        ),
    )


def reconcile_twilio_sms_status(
    callback: TwilioSmsStatusCallback,
    *,
    store: MonitoringStore,
    updated_at: str | None = None,
    maximum_attempts: int = 5,
) -> DeliveryReconciliationOutcome:
    """Atomically advance one delivery record without regressing its state."""

    for _ in range(maximum_attempts):
        located = store.get_notification_by_provider_delivery(
            "twilio", callback.message_sid
        )
        if located is None:
            return _ignored_outcome(callback, reason="UNKNOWN_DELIVERY")
        decision_id, current = located
        previous_provider_status = str(current.get("provider_status") or "accepted")

        if previous_provider_status == callback.message_status:
            return _ignored_outcome(
                callback,
                reason="DUPLICATE",
                record=current,
                decision_id=decision_id,
                duplicate=True,
            )
        if previous_provider_status in _TERMINAL_PROVIDER_STATUSES or current.get(
            "status"
        ) in {"delivered", "failed"}:
            return _ignored_outcome(
                callback,
                reason="TERMINAL_STATUS",
                record=current,
                decision_id=decision_id,
            )
        if (
            callback.message_status not in _TERMINAL_PROVIDER_STATUSES
            and _PROGRESS_RANK.get(callback.message_status, -1)
            <= _PROGRESS_RANK.get(previous_provider_status, -1)
        ):
            return _ignored_outcome(
                callback,
                reason="STALE_STATUS",
                record=current,
                decision_id=decision_id,
            )

        replacement = dict(current)
        action_status = _action_status(callback.message_status)
        replacement.update(
            {
                "status": action_status,
                "provider_status": callback.message_status,
                "delivery_updated_at": updated_at or _now_utc(),
                "error_code": (
                    f"TWILIO_{callback.error_code}"
                    if callback.error_code is not None
                    else None
                ),
            }
        )
        if store.compare_and_set_notification(
            decision_id,
            expected=current,
            replacement=replacement,
        ):
            return DeliveryReconciliationOutcome(
                provider_delivery_id=callback.message_sid,
                found=True,
                applied=True,
                decision_id=decision_id,
                notification_id=str(current["notification_id"]),
                previous_provider_status=previous_provider_status,
                provider_status=callback.message_status,
                action_status=action_status,
            )
    raise RuntimeError("Concurrent delivery reconciliation did not converge")


def create_notification_webhook_app(
    *,
    store: MonitoringStore | None = None,
    enabled: bool | None = None,
    account_sid: str | None = None,
    auth_token: str | None = None,
    public_callback_url: str | None = None,
    test_api_enabled: bool | None = None,
) -> FastAPI:
    resolved_store = store or DynamoMonitoringStateStore.from_environment()
    resolved_enabled = (
        enabled
        if enabled is not None
        else os.getenv("TWILIO_WEBHOOK_ENABLED", "false").lower() == "true"
    )
    resolved_account_sid = account_sid or os.getenv("TWILIO_ACCOUNT_SID", "")
    resolved_auth_token = (
        auth_token
        or os.getenv("TWILIO_AUTH_TOKEN", "")
        or os.getenv("TWILIO_AUTH_KEY", "")
    )
    resolved_callback_url = public_callback_url or os.getenv(
        "TWILIO_STATUS_CALLBACK_URL", ""
    )
    resolved_test_api = (
        test_api_enabled
        if test_api_enabled is not None
        else os.getenv("NOTIFICATION_WEBHOOK_TEST_API_ENABLED", "false").lower()
        == "true"
    )
    if resolved_enabled and (
        not resolved_account_sid
        or not resolved_auth_token
        or not resolved_callback_url
    ):
        raise RuntimeError(
            "Enabled Twilio webhook needs account SID, Auth Token, and callback URL"
        )
    validator = RequestValidator(resolved_auth_token) if resolved_enabled else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if isinstance(resolved_store, DynamoMonitoringStateStore):
            await asyncio.to_thread(resolved_store.ensure_table)
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False

    app = FastAPI(
        title="Travel Twilio SMS Delivery Webhook",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.ready = False
    install_telemetry_routes(app, service_name="notification-webhook-service")

    @app.get("/health/live", tags=["health"])
    async def health() -> dict[str, str | bool]:
        return {"status": "ok", "enabled": resolved_enabled}

    @app.post("/v1/webhooks/twilio/status", status_code=204)
    async def twilio_status(request: Request) -> Response:
        if not resolved_enabled or validator is None:
            raise HTTPException(status_code=404, detail="Webhook is disabled")
        form = await request.form()
        signature = request.headers.get("x-twilio-signature", "")
        if not signature or not validator.validate(
            resolved_callback_url, form, signature
        ):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")
        raw_error = form.get("ErrorCode")
        try:
            callback = TwilioSmsStatusCallback(
                account_sid=str(form.get("AccountSid") or ""),
                message_sid=str(form.get("MessageSid") or ""),
                message_status=str(form.get("MessageStatus") or ""),
                error_code=(raw_error if raw_error not in {None, ""} else None),
            )
        except (ValidationError, ValueError) as error:
            raise HTTPException(
                status_code=422, detail="Invalid Twilio status callback"
            ) from error
        if callback.account_sid != resolved_account_sid:
            raise HTTPException(status_code=403, detail="Account mismatch")
        await asyncio.to_thread(
            reconcile_twilio_sms_status,
            callback,
            store=resolved_store,
        )
        return Response(status_code=204)

    @app.get("/v1/test/deliveries/{provider_delivery_id}")
    async def test_delivery(provider_delivery_id: str) -> NotificationActionRecord:
        if not resolved_test_api:
            raise HTTPException(status_code=404, detail="Not found")
        located = await asyncio.to_thread(
            resolved_store.get_notification_by_provider_delivery,
            "twilio",
            provider_delivery_id,
        )
        if located is None:
            raise HTTPException(status_code=404, detail="Delivery not found")
        return NotificationActionRecord.model_validate(located[1])

    return app


app = create_notification_webhook_app()
