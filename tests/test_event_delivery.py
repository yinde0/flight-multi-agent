from __future__ import annotations

import asyncio
import json

from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from flight_agent.eval_service import commit_evaluation
from flight_agent.event_delivery import (
    DISRUPTION_CANDIDATE_SUBJECT,
    DurableEventEnvelope,
    candidate_outbox,
    consume_event_trace,
    decode_envelope,
    publish_durable_event,
    publish_pending_outbox,
    retry_or_quarantine,
)
from flight_agent.telemetry import current_trace_id


def candidate() -> dict[str, Any]:
    return {
        "candidate_id": "cand-v8-unit",
        "trip_id": "trip-v8-unit",
        "leg_id": "leg-v8-unit",
        "observed_at": "2026-09-15T06:05:00Z",
    }


class FakeJetStream:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def publish(self, subject, payload, **kwargs):
        self.calls.append({"subject": subject, "payload": payload, **kwargs})
        return SimpleNamespace(stream="TRAVEL_DISRUPTIONS_V1", seq=1)


def test_durable_publish_wraps_payload_and_sets_deduplication_header() -> None:
    jetstream = FakeJetStream()
    record = candidate_outbox(candidate())

    asyncio.run(publish_durable_event(jetstream, record))

    assert len(jetstream.calls) == 1
    call = jetstream.calls[0]
    assert call["subject"] == DISRUPTION_CANDIDATE_SUBJECT
    assert call["headers"] == {"Nats-Msg-Id": "cand-v8-unit"}
    envelope = DurableEventEnvelope.model_validate_json(call["payload"])
    assert envelope.event_type == "disruption_candidate"
    assert envelope.payload == candidate()


def test_outbox_retry_preserves_w3c_trace_lineage() -> None:
    span_context = SpanContext(
        trace_id=int("abcdefabcdefabcdefabcdefabcdefab", 16),
        span_id=int("1111111111111111", 16),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    token = otel_context.attach(
        trace.set_span_in_context(NonRecordingSpan(span_context))
    )
    try:
        record = candidate_outbox(candidate())
    finally:
        otel_context.detach(token)

    assert record["trace_headers"]["traceparent"].startswith(
        "00-abcdefabcdefabcdefabcdefabcdefab-"
    )
    jetstream = FakeJetStream()
    asyncio.run(publish_durable_event(jetstream, record))
    published = jetstream.calls[0]["headers"]
    assert published["traceparent"].startswith(
        "00-abcdefabcdefabcdefabcdefabcdefab-"
    )
    assert published["Nats-Msg-Id"] == "cand-v8-unit"

    message = SimpleNamespace(
        data=b"{}",
        headers=published,
        metadata=SimpleNamespace(num_delivered=2),
    )
    with consume_event_trace(
        message,
        service_name="eval-agent",
        operation="messaging.consume.disruption_candidate",
    ):
        assert current_trace_id() == "abcdefabcdefabcdefabcdefabcdefab"


def test_decode_rejects_event_on_the_wrong_consumer() -> None:
    envelope = DurableEventEnvelope(
        event_id="cand-v8-unit",
        event_type="disruption_candidate",
        occurred_at="2026-09-15T06:05:00Z",
        payload=candidate(),
    )
    message = SimpleNamespace(data=envelope.model_dump_json().encode("utf-8"))

    decoded = decode_envelope(message, expected_type="disruption_candidate")

    assert decoded.event_id == "cand-v8-unit"
    with pytest.raises(ValueError):
        decode_envelope(message, expected_type="disruption_confirmed")


class OutboxStore:
    def __init__(self) -> None:
        self.records = [candidate_outbox(candidate())]
        self.deleted: list[tuple[str, str]] = []
        self.failures: list[tuple[str, str]] = []

    def list_outbox(self, event_type, *, maximum):
        assert event_type == "disruption_candidate"
        assert maximum == 20
        return list(self.records)

    def delete_outbox(self, event_type, event_id):
        self.deleted.append((event_type, event_id))

    def note_outbox_failure(self, event_type, event_id):
        self.failures.append((event_type, event_id))


def test_outbox_is_deleted_only_after_publish_acknowledges() -> None:
    store = OutboxStore()

    async def publish(record):
        assert record["event_id"] == "cand-v8-unit"

    count = asyncio.run(
        publish_pending_outbox(
            store=store,
            event_type="disruption_candidate",
            publish=publish,
        )
    )

    assert count == 1
    assert store.deleted == [("disruption_candidate", "cand-v8-unit")]
    assert store.failures == []


def test_outbox_remains_when_publish_fails() -> None:
    store = OutboxStore()

    async def fail(record):
        del record
        raise RuntimeError("NATS unavailable")

    count = asyncio.run(
        publish_pending_outbox(
            store=store,
            event_type="disruption_candidate",
            publish=fail,
        )
    )

    assert count == 0
    assert store.deleted == []
    assert store.failures == [("disruption_candidate", "cand-v8-unit")]


class AtomicEvalStore:
    def __init__(self) -> None:
        self.atomic: dict[str, Any] | None = None

    def commit_evaluation_with_outbox(self, **kwargs):
        self.atomic = kwargs

    def put_decision(self, *args):
        raise AssertionError("legacy writes must not run after atomic commit")


def test_eval_uses_one_atomic_decision_and_outbox_commit() -> None:
    store = AtomicEvalStore()
    decision = {"decision_id": "decision-v8-unit"}
    event = {"decision_id": "decision-v8-unit"}

    commit_evaluation(
        candidate_id="cand-v8-unit",
        decision=decision,
        confirmed_event=event,
        episode_key="trip:leg:CANCELLATION",
        notified_band=3,
        store=store,
    )

    assert store.atomic == {
        "candidate_id": "cand-v8-unit",
        "decision": decision,
        "confirmed_event": event,
        "episode_key": "trip:leg:CANCELLATION",
        "notified_band": 3,
    }


def test_eval_advisory_is_included_in_atomic_commit_without_becoming_authority() -> None:
    store = AtomicEvalStore()
    decision = {"decision_id": "decision-v8-unit", "verdict": "NOTIFY"}
    advisory = {
        "status": "disagreed",
        "policy_verdict": "NOTIFY",
        "advisory": {"recommended_verdict": "SUPPRESS"},
        "authoritative_source": "deterministic_policy",
    }

    commit_evaluation(
        candidate_id="cand-v8-unit",
        decision=decision,
        confirmed_event={"decision_id": "decision-v8-unit"},
        episode_key="trip:leg:DELAY",
        notified_band=2,
        store=store,
        advisory=advisory,
    )

    assert store.atomic is not None
    assert store.atomic["advisory"] == advisory
    assert store.atomic["decision"]["verdict"] == "NOTIFY"


class RetryMessage:
    def __init__(self, attempts: int) -> None:
        self.data = json.dumps({"bad": True}).encode("utf-8")
        self.metadata = SimpleNamespace(num_delivered=attempts)
        self.nak_delays: list[float] = []
        self.terminated = False

    async def nak(self, delay=None):
        self.nak_delays.append(delay)

    async def term(self):
        self.terminated = True


class DeadLetterStore:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def put_dead_letter(self, **record):
        self.records.append(record)


def test_exhausted_delivery_is_quarantined_and_terminated(monkeypatch) -> None:
    monkeypatch.setenv("EVENT_MAX_DELIVERIES", "3")
    message = RetryMessage(attempts=3)
    store = DeadLetterStore()

    asyncio.run(
        retry_or_quarantine(
            message,
            store=store,
            consumer="travel-eval-agent-v1",
            event_id="cand-v8-unit",
            payload=candidate(),
            error_code="EVALUATION_FAILED",
        )
    )

    assert message.terminated is True
    assert message.nak_delays == []
    assert store.records[0]["attempts"] == 3
    assert store.records[0]["error_code"] == "EVALUATION_FAILED"
