from __future__ import annotations

import asyncio
import json
import os
import time

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException

from flight_agent.eval_service import connect_nats
from flight_agent.monitoring_events import DISRUPTION_CONFIRMED_SUBJECT
from flight_agent.monitoring_store import DynamoMonitoringStateStore, MonitoringStore
from flight_agent.notification_contracts import (
    ConfirmedDisruptionEvent,
    EvalApproval,
    NotificationActionRecord,
    NotificationCommand,
)
from flight_agent.notification_mcp_client import (
    NotificationGateway,
    StreamableHttpNotificationMcpClient,
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _verified_decision(
    event: ConfirmedDisruptionEvent,
    store: MonitoringStore,
    *,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """Wait briefly for Eval's commit, then match all authority-bearing fields."""
    deadline = time.monotonic() + timeout_seconds
    decision = None
    confirmed = None
    while True:
        decision = store.get_decision(event.candidate_id)
        confirmed = store.get_confirmed_event(event.candidate_id)
        if decision is not None and confirmed is not None:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if decision is None or confirmed is None:
        return None
    expected = {
        "candidate_id": event.candidate_id,
        "decision_id": event.decision_id,
        "trip_id": event.trip_id,
        "leg_id": event.leg_id,
        "verdict": event.verdict,
        "reason_codes": event.reason_codes,
    }
    if any(decision.get(key) != value for key, value in expected.items()):
        return None
    if any(confirmed.get(key) != value for key, value in expected.items()):
        return None
    if decision.get("verdict") == "SUPPRESS":
        return None
    return decision


def process_confirmed_event(
    event_payload: dict[str, Any],
    *,
    store: MonitoringStore,
    notifier: NotificationGateway,
    authority_timeout_seconds: float = 3.0,
) -> NotificationActionRecord:
    event = ConfirmedDisruptionEvent.model_validate(event_payload)
    existing = store.get_notification(event.decision_id)
    if existing is not None and existing.get("status") in {"delivered", "duplicate"}:
        return NotificationActionRecord.model_validate(existing)

    decision = _verified_decision(
        event, store, timeout_seconds=authority_timeout_seconds
    )
    suffix = event.decision_id.removeprefix("decision-")
    notification_id = f"notification-{suffix}"
    idempotency_key = f"notification:{event.decision_id}"
    if decision is None:
        return NotificationActionRecord(
            notification_id=notification_id,
            candidate_id=event.candidate_id,
            decision_id=event.decision_id,
            trip_id=event.trip_id,
            leg_id=event.leg_id,
            verdict=event.verdict,
            status="rejected",
            idempotency_key=idempotency_key,
            recorded_at=_now_utc(),
            error_code="EVAL_AUTHORITY_MISMATCH",
        )

    command = NotificationCommand(
        notification_id=notification_id,
        idempotency_key=idempotency_key,
        trip_id=event.trip_id,
        leg_id=event.leg_id,
        recipient_ref=f"traveler:{event.trip_id}",
        template_variables={
            "category": event.category,
            "trip_id": event.trip_id,
            "leg_id": event.leg_id,
        },
        search_requested=event.verdict == "NOTIFY_AND_SEARCH",
        approval=EvalApproval(
            candidate_id=event.candidate_id,
            decision_id=event.decision_id,
            verdict=event.verdict,
            policy_version=str(decision["policy_version"]),
            reason_codes=event.reason_codes,
            decided_at=str(decision["decided_at"]),
        ),
    )
    try:
        receipt = notifier.send_notification(command)
        record = NotificationActionRecord(
            notification_id=notification_id,
            candidate_id=event.candidate_id,
            decision_id=event.decision_id,
            trip_id=event.trip_id,
            leg_id=event.leg_id,
            verdict=event.verdict,
            status=receipt.status,
            idempotency_key=idempotency_key,
            provider=receipt.provider,
            provider_delivery_id=receipt.provider_delivery_id,
            recorded_at=receipt.delivered_at,
        )
    except Exception:
        record = NotificationActionRecord(
            notification_id=notification_id,
            candidate_id=event.candidate_id,
            decision_id=event.decision_id,
            trip_id=event.trip_id,
            leg_id=event.leg_id,
            verdict=event.verdict,
            status="failed",
            idempotency_key=idempotency_key,
            recorded_at=_now_utc(),
            error_code="NOTIFICATION_MCP_FAILED",
        )
    store.put_notification(event.decision_id, record.model_dump(mode="json"))
    return record


def create_notification_action_app(
    *,
    store: MonitoringStore | None = None,
    notifier: NotificationGateway | None = None,
) -> FastAPI:
    resolved_store = store or DynamoMonitoringStateStore.from_environment()
    resolved_notifier = notifier or StreamableHttpNotificationMcpClient(
        os.getenv("NOTIFICATION_MCP_URL", "http://127.0.0.1:8007/mcp")
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if isinstance(resolved_store, DynamoMonitoringStateStore):
            await asyncio.to_thread(resolved_store.ensure_table)
        connection = await connect_nats(
            os.getenv("NATS_URL", "nats://127.0.0.1:4222")
        )

        async def handle_confirmed(message) -> None:
            try:
                event = json.loads(message.data.decode("utf-8"))
                await asyncio.to_thread(
                    process_confirmed_event,
                    event,
                    store=resolved_store,
                    notifier=resolved_notifier,
                )
            except Exception:
                # Invalid or forged events are rejected without reaching MCP.
                return

        subscription = await connection.subscribe(
            DISRUPTION_CONFIRMED_SUBJECT,
            queue="travel-notification-action-v1",
            cb=handle_confirmed,
        )
        await connection.flush(timeout=3)
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            await subscription.unsubscribe()
            await connection.drain()

    app = FastAPI(
        title="Travel Post-Eval Notification Action Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.ready = False

    @app.get("/health/live", tags=["health"])
    async def health() -> dict[str, str]:
        if not app.state.ready:
            raise HTTPException(status_code=503, detail="Action service is starting")
        return {"status": "ok"}

    return app


app = create_notification_action_app()
