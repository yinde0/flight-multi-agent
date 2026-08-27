from __future__ import annotations

import asyncio
import os
import secrets

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from flight_agent.eval_service import connect_nats
from flight_agent.event_delivery import (
    DISRUPTION_CANDIDATE_SUBJECT,
    DISRUPTION_CONFIRMED_SUBJECT,
    EVAL_CONSUMER,
    EVENT_STREAM_NAME,
    NOTIFICATION_CONSUMER,
    SEARCH_CONSUMER,
    candidate_outbox,
    confirmed_outbox,
    ensure_event_stream,
    publish_durable_event,
)
from flight_agent.monitoring_store import DynamoMonitoringStateStore, MonitoringStore
from flight_agent.notification_contracts import ConfirmedDisruptionEvent
from flight_agent.telemetry import hash_reference, install_telemetry_routes, traced


KNOWN_CONSUMERS = (EVAL_CONSUMER, NOTIFICATION_CONSUMER, SEARCH_CONSUMER)


class RedriveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{2,99}$")
    operator_ref: str = Field(min_length=3, max_length=100)
    reason: str = Field(min_length=10, max_length=500)


def _require_operator(request: Request) -> None:
    configured = os.getenv("OPS_API_TOKEN", "")
    supplied = request.headers.get("x-ops-token", "")
    if not configured:
        raise HTTPException(status_code=503, detail="Operations access is disabled")
    if not supplied or not secrets.compare_digest(configured, supplied):
        raise HTTPException(status_code=401, detail="Invalid operations credential")


def _validate_consumer(consumer: str) -> str:
    if consumer not in KNOWN_CONSUMERS:
        raise HTTPException(status_code=404, detail="Unknown durable consumer")
    return consumer


def _record_for_redrive(
    consumer: str, event_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    if consumer == EVAL_CONSUMER:
        if str(payload.get("candidate_id") or "") != event_id:
            raise ValueError("Candidate dead letter does not match its event ID")
        for key in ("trip_id", "leg_id", "observed_at"):
            if not str(payload.get(key) or ""):
                raise ValueError("Candidate dead letter is missing required evidence")
        record = candidate_outbox(payload)
        if record["subject"] != DISRUPTION_CANDIDATE_SUBJECT:
            raise ValueError("Candidate subject mismatch")
        return record
    event = ConfirmedDisruptionEvent.model_validate(payload)
    if event.decision_id != event_id:
        raise ValueError("Confirmed dead letter does not match its event ID")
    record = confirmed_outbox(event.model_dump(mode="json"))
    if record["subject"] != DISRUPTION_CONFIRMED_SUBJECT:
        raise ValueError("Confirmed subject mismatch")
    return record


def _metric_lines(
    *,
    outboxes: dict[str, int],
    dead_letters: dict[str, int],
    consumers: dict[str, dict[str, int] | None],
) -> str:
    lines = [
        "# HELP travel_outbox_pending Pending transactional outbox records.",
        "# TYPE travel_outbox_pending gauge",
    ]
    for event_type, value in sorted(outboxes.items()):
        lines.append(
            f'travel_outbox_pending{{event_type="{event_type}"}} {value}'
        )
    lines.extend(
        [
            "# HELP travel_dead_letters_active Active quarantined events.",
            "# TYPE travel_dead_letters_active gauge",
        ]
    )
    for consumer, value in sorted(dead_letters.items()):
        lines.append(
            f'travel_dead_letters_active{{consumer="{consumer}"}} {value}'
        )
    lines.extend(
        [
            "# HELP travel_jetstream_consumer_pending Messages awaiting delivery.",
            "# TYPE travel_jetstream_consumer_pending gauge",
            "# HELP travel_jetstream_consumer_ack_pending Delivered messages awaiting ACK.",
            "# TYPE travel_jetstream_consumer_ack_pending gauge",
        ]
    )
    for consumer, values in sorted(consumers.items()):
        if values is None:
            continue
        lines.append(
            "travel_jetstream_consumer_pending"
            f'{{consumer="{consumer}"}} {values["pending"]}'
        )
        lines.append(
            "travel_jetstream_consumer_ack_pending"
            f'{{consumer="{consumer}"}} {values["ack_pending"]}'
        )
    return "\n".join(lines) + "\n"


def create_operations_app(store: MonitoringStore | None = None) -> FastAPI:
    resolved_store = store or DynamoMonitoringStateStore.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if isinstance(resolved_store, DynamoMonitoringStateStore):
            await asyncio.to_thread(resolved_store.ensure_table)
        connection = await connect_nats(
            os.getenv("NATS_URL", "nats://127.0.0.1:4222")
        )
        jetstream = connection.jetstream()
        await ensure_event_stream(jetstream)
        app.state.jetstream = jetstream
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            await connection.drain()

    app = FastAPI(
        title="Travel Disruption Operations",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.ready = False
    install_telemetry_routes(
        app, service_name="operations-service", include_metrics=False
    )

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        if not app.state.ready:
            raise HTTPException(status_code=503, detail="Operations service is starting")
        return {"status": "ok"}

    async def operational_state() -> tuple[
        dict[str, int],
        dict[str, int],
        dict[str, dict[str, int] | None],
    ]:
        outboxes: dict[str, int] = {}
        dead_letters: dict[str, int] = {}
        consumers: dict[str, dict[str, int] | None] = {}
        for event_type in ("disruption_candidate", "disruption_confirmed"):
            outboxes[event_type] = await asyncio.to_thread(
                resolved_store.outbox_count, event_type
            )
        for consumer in KNOWN_CONSUMERS:
            dead_letters[consumer] = await asyncio.to_thread(
                resolved_store.dead_letter_count, consumer
            )
            try:
                info = await app.state.jetstream.consumer_info(
                    EVENT_STREAM_NAME, consumer
                )
                consumers[consumer] = {
                    "pending": int(info.num_pending or 0),
                    "ack_pending": int(info.num_ack_pending or 0),
                }
            except Exception:
                consumers[consumer] = None
        return outboxes, dead_letters, consumers

    @app.get("/metrics", include_in_schema=False)
    async def operations_metrics() -> PlainTextResponse:
        outboxes, dead_letters, consumers = await operational_state()
        return PlainTextResponse(
            _metric_lines(
                outboxes=outboxes,
                dead_letters=dead_letters,
                consumers=consumers,
            ),
            media_type="text/plain; version=0.0.4",
        )

    @app.get("/v1/operations/status", tags=["operations"])
    async def operations_status(request: Request) -> dict[str, Any]:
        _require_operator(request)
        outboxes, dead_letters, consumers = await operational_state()
        return {
            "outbox_pending": outboxes,
            "dead_letters_active": dead_letters,
            "consumers": consumers,
        }

    @app.get(
        "/v1/operations/dead-letters/{consumer}", tags=["operations"]
    )
    async def list_dead_letters(
        consumer: str, request: Request
    ) -> dict[str, Any]:
        _require_operator(request)
        _validate_consumer(consumer)
        records = await asyncio.to_thread(
            resolved_store.list_dead_letters, consumer, active_only=True
        )
        return {"consumer": consumer, "count": len(records), "events": records}

    @app.post(
        "/v1/operations/dead-letters/{consumer}/{event_id}/redrive",
        tags=["operations"],
    )
    @traced(
        "operations.redrive",
        service_name="operations-service",
        kind="tool",
        attributes=lambda consumer, event_id, command, request: {
            "travel.consumer": consumer,
            "travel.event_ref": hash_reference(event_id),
        },
        result_outcome=lambda result: result.get("status", "unknown"),
    )
    async def redrive_dead_letter(
        consumer: str,
        event_id: str,
        command: RedriveRequest,
        request: Request,
    ) -> dict[str, str]:
        _require_operator(request)
        _validate_consumer(consumer)
        dead_letter = await asyncio.to_thread(
            resolved_store.get_dead_letter, consumer, event_id
        )
        if dead_letter is None:
            raise HTTPException(status_code=404, detail="Dead letter not found")
        if dead_letter.get("status", "active") != "active":
            return {
                "status": "already_redriven",
                "event_id": event_id,
                "request_id": str(dead_letter.get("redrive_request_id") or ""),
            }
        try:
            record = _record_for_redrive(
                consumer, event_id, dict(dead_letter["payload"])
            )
        except Exception as error:
            raise HTTPException(
                status_code=422, detail="Dead letter payload is not safe to re-drive"
            ) from error
        claimed, claim = await asyncio.to_thread(
            resolved_store.claim_redrive,
            consumer=consumer,
            event_id=event_id,
            request_id=command.request_id,
            operator_ref=command.operator_ref,
            reason=command.reason,
        )
        if not claimed:
            return {
                "status": f"already_{claim.get('status', 'claimed')}",
                "event_id": event_id,
                "request_id": command.request_id,
            }
        try:
            await publish_durable_event(
                app.state.jetstream,
                record,
                message_id=(
                    f"redrive:{consumer}:{event_id}:{command.request_id}"
                ),
            )
            await asyncio.to_thread(
                resolved_store.mark_dead_letter_redriven,
                consumer=consumer,
                event_id=event_id,
                request_id=command.request_id,
            )
            await asyncio.to_thread(
                resolved_store.finish_redrive,
                consumer=consumer,
                event_id=event_id,
                request_id=command.request_id,
                status="published",
            )
        except Exception as error:
            await asyncio.to_thread(
                resolved_store.finish_redrive,
                consumer=consumer,
                event_id=event_id,
                request_id=command.request_id,
                status="failed",
                error_code="REDRIVE_PUBLISH_FAILED",
            )
            raise HTTPException(status_code=503, detail="Re-drive publication failed") from error
        return {
            "status": "published",
            "event_id": event_id,
            "request_id": command.request_id,
        }

    return app


app = create_operations_app()
