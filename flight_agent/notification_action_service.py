from __future__ import annotations

import asyncio
import os
import time

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException

from flight_agent.communication_a2a_client import (
    A2ACommunicationAgentClient,
    CommunicationAgentGateway,
)
from flight_agent.disruption_explanation import (
    DisruptionExplanation,
    DisruptionExplanationRequest,
    deterministic_explanation,
)
from flight_agent.event_delivery import (
    DISRUPTION_CONFIRMED_SUBJECT,
    NOTIFICATION_CONSUMER,
    consume_event_trace,
    decode_envelope,
    ensure_event_stream,
    fallback_event_id,
    quarantine_message,
    retry_or_quarantine,
    subscribe_durable,
)
from flight_agent.eval_service import connect_nats
from flight_agent.monitoring_store import DynamoMonitoringStateStore, MonitoringStore
from flight_agent.notification_contracts import (
    ConfirmedDisruptionEvent,
    EvalApproval,
    NotificationActionRecord,
    NotificationCommand,
)
from flight_agent.notification_recipient import (
    HttpNotificationRecipientResolver,
    NotificationRecipientResolver,
)
from flight_agent.notification_mcp_client import (
    NotificationGateway,
    StreamableHttpNotificationMcpClient,
)
from flight_agent.notification_errors import NotificationSubmissionError
from flight_agent.telemetry import hash_reference, install_telemetry_routes, traced


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


def notification_agent_trace_input(
    event_payload: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
    return {
        "task": "Verify Eval approval and notify the traveler once.",
        "event": {
            "candidate_ref": hash_reference(event_payload.get("candidate_id", "")),
            "decision_ref": hash_reference(event_payload.get("decision_id", "")),
            "trip_ref": hash_reference(event_payload.get("trip_id", "")),
            "leg_ref": hash_reference(event_payload.get("leg_id", "")),
            "category": event_payload.get("category"),
            "verdict": event_payload.get("verdict"),
            "reason_codes": event_payload.get("reason_codes", []),
        },
        "delivery_provider": kwargs.get("delivery_provider", "recording"),
    }


def notification_agent_trace_output(
    result: NotificationActionRecord,
) -> dict[str, Any]:
    return {
        "status": result.status,
        "verdict": result.verdict,
        "provider": result.provider,
        "provider_status": result.provider_status,
        "error_code": result.error_code,
        "submission_failure": (
            result.submission_failure.model_dump(mode="json", exclude_none=True)
            if result.submission_failure else None
        ),
        "explanation": {
            "status": result.explanation_status,
            "source": result.explanation_source,
            "model": result.explanation_model,
            "prompt_version": result.explanation_prompt_version,
            "error_code": result.explanation_error_code,
        },
    }


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        numeric = float(value)
        converted = int(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return converted if numeric == converted else None


def _explanation_request(
    event: ConfirmedDisruptionEvent,
    candidate: dict[str, Any] | None,
) -> DisruptionExplanationRequest:
    evidence = candidate or {}
    weather = evidence.get("weather_risk_level")
    if weather not in {"none", "low", "moderate", "severe"}:
        weather = None
    corroborated = evidence.get("corroborated_by_weather")
    if not isinstance(corroborated, bool):
        corroborated = None
    return DisruptionExplanationRequest(
        category=event.category,
        verdict=event.verdict,
        reason_codes=event.reason_codes,
        delay_minutes=_optional_int(evidence.get("delay_minutes")),
        connection_buffer_minutes=_optional_int(
            evidence.get("connection_buffer_minutes")
        ),
        minimum_connection_minutes=_optional_int(
            evidence.get("minimum_connection_minutes")
        ),
        weather_risk_level=weather,
        corroborated_by_weather=corroborated,
        search_requested=event.verdict == "NOTIFY_AND_SEARCH",
    )


def _friendly_explanation(
    request: DisruptionExplanationRequest,
    communicator: CommunicationAgentGateway | None,
) -> DisruptionExplanation:
    if communicator is None:
        return deterministic_explanation(request)
    try:
        return communicator.explain(request)
    except Exception:
        return deterministic_explanation(
            request, error_code="EXPLANATION_AGENT_UNAVAILABLE"
        )


@traced(
    "agent.orchestrator.notify_traveler",
    service_name="notification-action-service",
    kind="chain",
    attributes=lambda event_payload, **kwargs: {
        "travel.decision_ref": hash_reference(
            event_payload.get("decision_id", "")
        ),
        "travel.trip_ref": hash_reference(event_payload.get("trip_id", "")),
    },
    result_outcome=lambda result: result.status,
    content_input=notification_agent_trace_input,
    content_output=notification_agent_trace_output,
)
def process_confirmed_event(
    event_payload: dict[str, Any],
    *,
    store: MonitoringStore,
    notifier: NotificationGateway,
    communicator: CommunicationAgentGateway | None = None,
    recipient_resolver: NotificationRecipientResolver | None = None,
    delivery_provider: str = "recording",
    authority_timeout_seconds: float = 3.0,
) -> NotificationActionRecord:
    event = ConfirmedDisruptionEvent.model_validate(event_payload)
    existing = store.get_notification(event.decision_id)
    if existing is not None and existing.get("status") in {
        "accepted",
        "delivered",
        "duplicate",
    }:
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

    recipient_address = None
    channel = "push"
    if delivery_provider == "twilio":
        try:
            recipient = (
                recipient_resolver.get_recipient(event.trip_id)
                if recipient_resolver is not None
                else None
            )
        except Exception:
            recipient = None
        if recipient is None:
            record = NotificationActionRecord(
                notification_id=notification_id,
                candidate_id=event.candidate_id,
                decision_id=event.decision_id,
                trip_id=event.trip_id,
                leg_id=event.leg_id,
                verdict=event.verdict,
                status="rejected",
                idempotency_key=idempotency_key,
                recorded_at=_now_utc(),
                error_code="SMS_RECIPIENT_UNAVAILABLE",
            )
            store.put_notification(event.decision_id, record.model_dump(mode="json"))
            return record
        recipient_address = recipient.phone_e164
        channel = "sms"

    candidate_reader = getattr(store, "get_candidate", None)
    candidate = candidate_reader(event.candidate_id) if callable(candidate_reader) else None
    explanation = _friendly_explanation(
        _explanation_request(event, candidate), communicator
    )

    command = NotificationCommand(
        notification_id=notification_id,
        idempotency_key=idempotency_key,
        trip_id=event.trip_id,
        leg_id=event.leg_id,
        recipient_ref=f"traveler:{event.trip_id}",
        recipient_address=recipient_address,
        channel=channel,
        template_variables={
            "category": event.category,
            "trip_id": event.trip_id,
            "leg_id": event.leg_id,
            "friendly_message": explanation.message,
            "explanation_source": explanation.source,
            "explanation_prompt_version": explanation.prompt_version,
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
            provider_status=receipt.provider_status,
            recorded_at=receipt.delivered_at,
            friendly_message=explanation.message,
            explanation_status=explanation.status,
            explanation_source=explanation.source,
            explanation_model=explanation.model,
            explanation_prompt_version=explanation.prompt_version,
            explanation_error_code=explanation.error_code,
        )
    except NotificationSubmissionError as error:
        record = NotificationActionRecord(
            notification_id=notification_id,
            candidate_id=event.candidate_id,
            decision_id=event.decision_id,
            trip_id=event.trip_id,
            leg_id=event.leg_id,
            verdict=event.verdict,
            status="failed",
            idempotency_key=idempotency_key,
            provider=error.failure.provider,
            recorded_at=_now_utc(),
            error_code=error.failure.error_code,
            submission_failure=error.failure,
            friendly_message=explanation.message,
            explanation_status=explanation.status,
            explanation_source=explanation.source,
            explanation_model=explanation.model,
            explanation_prompt_version=explanation.prompt_version,
            explanation_error_code=explanation.error_code,
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
            friendly_message=explanation.message,
            explanation_status=explanation.status,
            explanation_source=explanation.source,
            explanation_model=explanation.model,
            explanation_prompt_version=explanation.prompt_version,
            explanation_error_code=explanation.error_code,
        )
    store.put_notification(event.decision_id, record.model_dump(mode="json"))
    return record


def create_notification_action_app(
    *,
    store: MonitoringStore | None = None,
    notifier: NotificationGateway | None = None,
    communicator: CommunicationAgentGateway | None = None,
    recipient_resolver: NotificationRecipientResolver | None = None,
    delivery_provider: str | None = None,
) -> FastAPI:
    resolved_store = store or DynamoMonitoringStateStore.from_environment()
    resolved_notifier = notifier or StreamableHttpNotificationMcpClient(
        os.getenv("NOTIFICATION_MCP_URL", "http://127.0.0.1:8007/mcp")
    )
    resolved_communicator = communicator or A2ACommunicationAgentClient(
        os.getenv("COMMUNICATION_AGENT_URL", "http://127.0.0.1:8017"),
        timeout_seconds=float(os.getenv("COMMUNICATION_AGENT_TIMEOUT_SECONDS", "35")),
    )
    resolved_delivery_provider = (
        delivery_provider
        if delivery_provider is not None
        else os.getenv("NOTIFICATION_PROVIDER", "recording").lower()
    )
    resolved_recipient_resolver = recipient_resolver
    if resolved_delivery_provider == "twilio" and resolved_recipient_resolver is None:
        resolved_recipient_resolver = HttpNotificationRecipientResolver(
            os.getenv("TRIP_ORCHESTRATOR_URL", "http://127.0.0.1:8011")
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if isinstance(resolved_store, DynamoMonitoringStateStore):
            await asyncio.to_thread(resolved_store.ensure_table)
        connection = await connect_nats(
            os.getenv("NATS_URL", "nats://127.0.0.1:4222")
        )
        jetstream = connection.jetstream()
        await ensure_event_stream(jetstream)

        async def process_confirmed_message(message) -> None:
            envelope = None
            try:
                envelope = decode_envelope(
                    message, expected_type="disruption_confirmed"
                )
                record = await asyncio.to_thread(
                    process_confirmed_event,
                    envelope.payload,
                    store=resolved_store,
                    notifier=resolved_notifier,
                    communicator=resolved_communicator,
                    recipient_resolver=resolved_recipient_resolver,
                    delivery_provider=resolved_delivery_provider,
                )
                if record.status in {"accepted", "delivered", "duplicate"}:
                    await message.ack_sync(timeout=3)
                elif record.status == "rejected" or (
                    record.submission_failure is not None
                    and not record.submission_failure.retryable
                ):
                    await quarantine_message(
                        message,
                        store=resolved_store,
                        consumer=NOTIFICATION_CONSUMER,
                        event_id=envelope.event_id,
                        payload=envelope.payload,
                        error_code=record.error_code
                        or "NOTIFICATION_EVENT_REJECTED",
                    )
                else:
                    await retry_or_quarantine(
                        message,
                        store=resolved_store,
                        consumer=NOTIFICATION_CONSUMER,
                        event_id=envelope.event_id,
                        payload=envelope.payload,
                        error_code=record.error_code
                        or "NOTIFICATION_ACTION_FAILED",
                    )
            except Exception:
                if envelope is None:
                    await quarantine_message(
                        message,
                        store=resolved_store,
                        consumer=NOTIFICATION_CONSUMER,
                        event_id=fallback_event_id(message),
                        payload={},
                        error_code="CONFIRMED_EVENT_INVALID",
                    )
                else:
                    await retry_or_quarantine(
                        message,
                        store=resolved_store,
                        consumer=NOTIFICATION_CONSUMER,
                        event_id=envelope.event_id,
                        payload=envelope.payload,
                        error_code="NOTIFICATION_ACTION_FAILED",
                    )

        async def handle_confirmed(message) -> None:
            with consume_event_trace(
                message,
                service_name="notification-action-service",
                operation="messaging.consume.disruption_confirmed",
            ):
                await process_confirmed_message(message)

        subscription = await subscribe_durable(
            jetstream,
            subject=DISRUPTION_CONFIRMED_SUBJECT,
            durable_name=NOTIFICATION_CONSUMER,
            callback=handle_confirmed,
        )
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
    install_telemetry_routes(app, service_name="notification-action-service")

    @app.get("/health/live", tags=["health"])
    async def health() -> dict[str, str]:
        if not app.state.ready:
            raise HTTPException(status_code=503, detail="Action service is starting")
        return {"status": "ok"}

    return app


app = create_notification_action_app()
