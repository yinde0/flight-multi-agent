from __future__ import annotations

import asyncio
import os

from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, HTTPException

from flight_agent.event_delivery import (
    DISRUPTION_CONFIRMED_SUBJECT,
    SEARCH_CONSUMER,
    consume_event_trace,
    decode_envelope,
    ensure_event_stream,
    fallback_event_id,
    quarantine_message,
    retry_or_quarantine,
    subscribe_durable,
)
from flight_agent.eval_service import connect_nats
from flight_agent.flight_search import rank_feasible_options
from flight_agent.flight_search_contracts import (
    FlightSearchActionRecord,
    FlightSearchCommand,
    SearchEvalApproval,
)
from flight_agent.flight_search_mcp_client import (
    FlightSearchGateway,
    StreamableHttpFlightSearchMcpClient,
)
from flight_agent.monitoring_store import DynamoMonitoringStateStore, MonitoringStore
from flight_agent.notification_action_service import _now_utc, _verified_decision
from flight_agent.notification_contracts import ConfirmedDisruptionEvent
from flight_agent.telemetry import hash_reference, install_telemetry_routes, traced
from travel_eval.clock import parse_timestamp


def _record(
    event: ConfirmedDisruptionEvent,
    *,
    status: str,
    error_code: str | None = None,
) -> FlightSearchActionRecord:
    suffix = event.decision_id.removeprefix("decision-")
    return FlightSearchActionRecord(
        search_id=f"search-{suffix}",
        candidate_id=event.candidate_id,
        decision_id=event.decision_id,
        trip_id=event.trip_id,
        leg_id=event.leg_id,
        verdict=event.verdict,
        status=status,
        idempotency_key=f"search:{event.decision_id}",
        recorded_at=_now_utc(),
        error_code=error_code,
    )


def search_agent_trace_input(
    event_payload: dict, **_kwargs
) -> dict[str, object]:
    return {
        "task": "Verify Eval approval and search for feasible rebooking options.",
        "event": {
            "candidate_ref": hash_reference(event_payload.get("candidate_id", "")),
            "decision_ref": hash_reference(event_payload.get("decision_id", "")),
            "trip_ref": hash_reference(event_payload.get("trip_id", "")),
            "leg_ref": hash_reference(event_payload.get("leg_id", "")),
            "category": event_payload.get("category"),
            "verdict": event_payload.get("verdict"),
            "reason_codes": event_payload.get("reason_codes", []),
        },
    }


def search_agent_trace_output(
    result: FlightSearchActionRecord,
) -> dict[str, object]:
    return {
        "status": result.status,
        "verdict": result.verdict,
        "provider": result.provider,
        "source_scope": result.source_scope,
        "alternative_count": len(result.alternatives),
        "availability_verified": result.availability_verified,
        "booking_authorized": result.booking_authorized,
        "error_code": result.error_code,
    }


@traced(
    "agent.orchestrator.search_rebooking",
    service_name="flight-search-action-service",
    kind="chain",
    attributes=lambda event_payload, **kwargs: {
        "travel.decision_ref": hash_reference(
            event_payload.get("decision_id", "")
        ),
        "travel.trip_ref": hash_reference(event_payload.get("trip_id", "")),
    },
    result_outcome=lambda result: result.status,
    content_input=search_agent_trace_input,
    content_output=search_agent_trace_output,
)
def process_search_event(
    event_payload: dict,
    *,
    store: MonitoringStore,
    search_gateway: FlightSearchGateway,
    authority_timeout_seconds: float = 3.0,
) -> FlightSearchActionRecord:
    """Verify Eval authority, perform read-only search, filter, rank, and audit."""
    event = ConfirmedDisruptionEvent.model_validate(event_payload)
    if event.verdict != "NOTIFY_AND_SEARCH":
        return _record(event, status="rejected", error_code="SEARCH_NOT_AUTHORIZED")

    existing = store.get_search(event.decision_id)
    if existing is not None and existing.get("status") in {"completed", "no_options"}:
        return FlightSearchActionRecord.model_validate(existing)

    decision = _verified_decision(
        event, store, timeout_seconds=authority_timeout_seconds
    )
    if decision is None or decision.get("verdict") != "NOTIFY_AND_SEARCH":
        return _record(event, status="rejected", error_code="EVAL_AUTHORITY_MISMATCH")

    observation = store.get_last_observation(event.trip_id, event.leg_id)
    if observation is None:
        record = _record(event, status="failed", error_code="SEARCH_CONTEXT_MISSING")
        store.put_search(event.decision_id, record.model_dump(mode="json"))
        return record

    try:
        scheduled = str(observation["departure"]["scheduled_at"])
        earliest_time = max(
            parse_timestamp(scheduled), parse_timestamp(event.published_at)
        )
        earliest = earliest_time.isoformat().replace("+00:00", "Z")
        latest = (earliest_time + timedelta(hours=12)).isoformat().replace(
            "+00:00", "Z"
        )
        suffix = event.decision_id.removeprefix("decision-")
        command = FlightSearchCommand(
            search_id=f"search-{suffix}",
            idempotency_key=f"search:{event.decision_id}",
            trip_id=event.trip_id,
            leg_id=event.leg_id,
            original_flight_iata=str(observation["flight_iata"]),
            origin=str(observation["departure_airport"]),
            destination=str(observation["destination_airport"]),
            departure_date=str(observation["flight_date"]),
            earliest_departure_at=earliest,
            latest_departure_at=latest,
            approval=SearchEvalApproval(
                candidate_id=event.candidate_id,
                decision_id=event.decision_id,
                verdict="NOTIFY_AND_SEARCH",
                policy_version=str(decision["policy_version"]),
                reason_codes=event.reason_codes,
                decided_at=str(decision["decided_at"]),
            ),
        )
    except Exception:
        record = _record(event, status="failed", error_code="SEARCH_CONTEXT_INVALID")
        store.put_search(event.decision_id, record.model_dump(mode="json"))
        return record

    try:
        result = search_gateway.search_flights(command)
        if (
            result.search_id != command.search_id
            or result.decision_id != event.decision_id
            or result.idempotency_key != command.idempotency_key
            or result.booking_guaranteed
            or result.booking_authorized
        ):
            raise ValueError("Flight search MCP returned mismatched authority")
        alternatives, rejected = rank_feasible_options(command, result)
        record = FlightSearchActionRecord(
            search_id=command.search_id,
            candidate_id=event.candidate_id,
            decision_id=event.decision_id,
            trip_id=event.trip_id,
            leg_id=event.leg_id,
            verdict="NOTIFY_AND_SEARCH",
            status="completed" if alternatives else "no_options",
            idempotency_key=command.idempotency_key,
            provider=result.provider,
            source_scope=result.source_scope,
            alternatives=alternatives,
            rejection_summary=rejected,
            availability_verified=result.availability_verified,
            recorded_at=result.searched_at,
        )
    except Exception:
        record = _record(event, status="failed", error_code="FLIGHT_SEARCH_MCP_FAILED")

    store.put_search(event.decision_id, record.model_dump(mode="json"))
    return record


def create_flight_search_action_app(
    *,
    store: MonitoringStore | None = None,
    search_gateway: FlightSearchGateway | None = None,
) -> FastAPI:
    resolved_store = store or DynamoMonitoringStateStore.from_environment()
    resolved_gateway = search_gateway or StreamableHttpFlightSearchMcpClient(
        os.getenv("FLIGHT_SEARCH_MCP_URL", "http://127.0.0.1:8009/mcp")
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
                    process_search_event,
                    envelope.payload,
                    store=resolved_store,
                    search_gateway=resolved_gateway,
                )
                if record.status in {"completed", "no_options"}:
                    await message.ack_sync(timeout=3)
                elif (
                    record.status == "rejected"
                    and record.error_code == "SEARCH_NOT_AUTHORIZED"
                ):
                    # A NOTIFY event is valid for notification but intentionally
                    # irrelevant to the search consumer.
                    await message.ack_sync(timeout=3)
                elif record.status == "rejected":
                    await quarantine_message(
                        message,
                        store=resolved_store,
                        consumer=SEARCH_CONSUMER,
                        event_id=envelope.event_id,
                        payload=envelope.payload,
                        error_code=record.error_code or "SEARCH_EVENT_REJECTED",
                    )
                else:
                    await retry_or_quarantine(
                        message,
                        store=resolved_store,
                        consumer=SEARCH_CONSUMER,
                        event_id=envelope.event_id,
                        payload=envelope.payload,
                        error_code=record.error_code or "SEARCH_ACTION_FAILED",
                    )
            except Exception:
                if envelope is None:
                    await quarantine_message(
                        message,
                        store=resolved_store,
                        consumer=SEARCH_CONSUMER,
                        event_id=fallback_event_id(message),
                        payload={},
                        error_code="CONFIRMED_EVENT_INVALID",
                    )
                else:
                    await retry_or_quarantine(
                        message,
                        store=resolved_store,
                        consumer=SEARCH_CONSUMER,
                        event_id=envelope.event_id,
                        payload=envelope.payload,
                        error_code="SEARCH_ACTION_FAILED",
                    )

        async def handle_confirmed(message) -> None:
            with consume_event_trace(
                message,
                service_name="flight-search-action-service",
                operation="messaging.consume.disruption_confirmed",
            ):
                await process_confirmed_message(message)

        subscription = await subscribe_durable(
            jetstream,
            subject=DISRUPTION_CONFIRMED_SUBJECT,
            durable_name=SEARCH_CONSUMER,
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
        title="Travel Post-Eval Flight Search Action Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.ready = False
    install_telemetry_routes(app, service_name="flight-search-action-service")

    @app.get("/health/live", tags=["health"])
    async def health() -> dict[str, str]:
        if not app.state.ready:
            raise HTTPException(status_code=503, detail="Action service is starting")
        return {"status": "ok"}

    return app


app = create_flight_search_action_app()
