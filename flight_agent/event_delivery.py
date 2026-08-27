from __future__ import annotations

import asyncio
import hashlib
import json
import os

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Literal

from nats.js import errors as js_errors
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from pydantic import BaseModel, ConfigDict, Field

from flight_agent.telemetry import (
    extracted_trace_context,
    trace_headers,
    trace_operation,
)


EVENT_STREAM_NAME = "TRAVEL_DISRUPTIONS_V1"
DISRUPTION_CANDIDATE_SUBJECT = "travel.disruption_candidate.v1"
DISRUPTION_CONFIRMED_SUBJECT = "travel.disruption_confirmed.v1"

EVAL_CONSUMER = "travel-eval-agent-v1"
NOTIFICATION_CONSUMER = "travel-notification-action-v1"
SEARCH_CONSUMER = "travel-flight-search-action-v1"

EventType = Literal["disruption_candidate", "disruption_confirmed"]


class DurableEventEnvelope(BaseModel):
    """Stable payload carried by JetStream; provider headers are not authority."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    event_id: str = Field(min_length=1)
    event_type: EventType
    occurred_at: str
    payload: dict[str, Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def outbox_record(
    *,
    event_id: str,
    event_type: EventType,
    subject: str,
    occurred_at: str,
    payload: dict[str, Any],
    producer_service: str,
) -> dict[str, Any]:
    record = {
        "event_id": event_id,
        "event_type": event_type,
        "subject": subject,
        "occurred_at": occurred_at,
        "payload": payload,
        "producer_service": producer_service,
    }
    active_headers = trace_headers()
    if active_headers:
        record["trace_headers"] = active_headers
    return record


def candidate_outbox(candidate: dict[str, Any]) -> dict[str, Any]:
    return outbox_record(
        event_id=str(candidate["candidate_id"]),
        event_type="disruption_candidate",
        subject=DISRUPTION_CANDIDATE_SUBJECT,
        occurred_at=str(candidate["observed_at"]),
        payload=candidate,
        producer_service="monitor-agent",
    )


def confirmed_outbox(event: dict[str, Any]) -> dict[str, Any]:
    return outbox_record(
        event_id=str(event["decision_id"]),
        event_type="disruption_confirmed",
        subject=DISRUPTION_CONFIRMED_SUBJECT,
        occurred_at=str(event["published_at"]),
        payload=event,
        producer_service="eval-agent",
    )


async def ensure_event_stream(jetstream) -> None:
    config = StreamConfig(
        name=EVENT_STREAM_NAME,
        description="Durable travel disruption candidates and confirmed decisions",
        subjects=[
            DISRUPTION_CANDIDATE_SUBJECT,
            DISRUPTION_CONFIRMED_SUBJECT,
        ],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_age=float(os.getenv("EVENT_STREAM_MAX_AGE_SECONDS", "1209600")),
        duplicate_window=float(
            os.getenv("EVENT_STREAM_DUPLICATE_WINDOW_SECONDS", "600")
        ),
    )
    try:
        await jetstream.stream_info(EVENT_STREAM_NAME)
    except js_errors.NotFoundError:
        try:
            await jetstream.add_stream(config=config)
        except js_errors.APIError:
            # More than one service may create the shared stream at startup.
            await jetstream.stream_info(EVENT_STREAM_NAME)


def consumer_config() -> ConsumerConfig:
    return ConsumerConfig(
        ack_policy=AckPolicy.EXPLICIT,
        ack_wait=float(os.getenv("EVENT_ACK_WAIT_SECONDS", "30")),
        max_deliver=int(os.getenv("EVENT_MAX_DELIVERIES", "5")),
        max_ack_pending=int(os.getenv("EVENT_MAX_ACK_PENDING", "100")),
    )


async def subscribe_durable(
    jetstream,
    *,
    subject: str,
    durable_name: str,
    callback,
):
    return await jetstream.subscribe(
        subject,
        queue=durable_name,
        stream=EVENT_STREAM_NAME,
        cb=callback,
        config=consumer_config(),
        manual_ack=True,
    )


async def publish_durable_event(
    jetstream,
    record: dict[str, Any],
    *,
    message_id: str | None = None,
):
    envelope = DurableEventEnvelope(
        event_id=str(record["event_id"]),
        event_type=str(record["event_type"]),
        occurred_at=str(record["occurred_at"]),
        payload=dict(record["payload"]),
    )
    producer_service = str(record.get("producer_service") or "event-publisher")
    stored_headers = record.get("trace_headers")
    with extracted_trace_context(
        stored_headers if isinstance(stored_headers, dict) else None
    ):
        with trace_operation(
            "messaging.publish",
            service_name=producer_service,
            kind="tool",
            attributes={
                "messaging.system": "nats",
                "messaging.destination.name": str(record["subject"]),
            },
        ):
            headers = {"Nats-Msg-Id": message_id or envelope.event_id}
            headers.update(trace_headers())
            return await jetstream.publish(
                str(record["subject"]),
                envelope.model_dump_json(exclude_none=True).encode("utf-8"),
                stream=EVENT_STREAM_NAME,
                headers=headers,
                timeout=float(os.getenv("EVENT_PUBLISH_TIMEOUT_SECONDS", "5")),
            )


def decode_envelope(message, *, expected_type: EventType) -> DurableEventEnvelope:
    raw = json.loads(message.data.decode("utf-8"))
    envelope = DurableEventEnvelope.model_validate(raw)
    if envelope.event_type != expected_type:
        raise ValueError("Durable event type does not match its consumer")
    return envelope


def delivery_attempt(message) -> int:
    try:
        return max(1, int(message.metadata.num_delivered))
    except Exception:
        return 1


@contextmanager
def consume_event_trace(
    message,
    *,
    service_name: str,
    operation: str,
) -> Iterator[None]:
    """Continue a producer trace for one JetStream delivery or redelivery."""

    headers = getattr(message, "headers", None)
    with extracted_trace_context(headers if hasattr(headers, "items") else None):
        with trace_operation(
            operation,
            service_name=service_name,
            kind="chain",
            attributes={
                "messaging.system": "nats",
                "messaging.delivery.attempt": delivery_attempt(message),
            },
        ):
            yield


def fallback_event_id(message) -> str:
    return "unreadable-" + hashlib.sha256(message.data).hexdigest()[:24]


async def quarantine_message(
    message,
    *,
    store,
    consumer: str,
    event_id: str,
    payload: dict[str, Any],
    error_code: str,
) -> None:
    writer = getattr(store, "put_dead_letter", None)
    if callable(writer):
        await asyncio.to_thread(
            writer,
            consumer=consumer,
            event_id=event_id,
            payload=payload,
            error_code=error_code,
            attempts=delivery_attempt(message),
        )
    await message.term()


async def retry_or_quarantine(
    message,
    *,
    store,
    consumer: str,
    event_id: str,
    payload: dict[str, Any],
    error_code: str,
) -> None:
    maximum = int(os.getenv("EVENT_MAX_DELIVERIES", "5"))
    if delivery_attempt(message) >= maximum:
        await quarantine_message(
            message,
            store=store,
            consumer=consumer,
            event_id=event_id,
            payload=payload,
            error_code=error_code,
        )
        return
    await message.nak(delay=float(os.getenv("EVENT_RETRY_DELAY_SECONDS", "2")))


async def publish_pending_outbox(
    *,
    store,
    event_type: EventType,
    publish,
    maximum: int = 20,
) -> int:
    """Publish acknowledged outbox records and retain every failed record."""

    reader = getattr(store, "list_outbox", None)
    deleter = getattr(store, "delete_outbox", None)
    failure_writer = getattr(store, "note_outbox_failure", None)
    if not callable(reader) or not callable(deleter):
        return 0
    records = await asyncio.to_thread(reader, event_type, maximum=maximum)
    published = 0
    for record in records:
        try:
            await publish(record)
            await asyncio.to_thread(
                deleter, event_type, str(record["event_id"])
            )
            published += 1
        except Exception:
            if callable(failure_writer):
                await asyncio.to_thread(
                    failure_writer, event_type, str(record["event_id"])
                )
    return published
