from __future__ import annotations

import asyncio
import copy
import hashlib

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from flight_agent.contracts import DocumentMetadata, ParseOutcome
from flight_agent.document_store import S3DocumentStore
from flight_agent.monitoring_contracts import (
    MonitoringPollOutcome,
    MonitoringPollRequest,
)
from flight_agent.parser import extract_pdf_text, parse_extracted_text
from flight_agent.trip_contracts import (
    DocumentObjectRef,
    NotificationRecipient,
    ScheduledLeg,
    SchedulerTickRequest,
    SmsNotificationPreference,
    StoredLegView,
    StoredTripView,
)
from flight_agent.trip_orchestrator import (
    TripDocumentConflictError,
    TripOrchestrator,
)
from flight_agent.trip_store import format_poll_identity, format_timestamp, next_poll_time
from flight_agent.telemetry import current_trace_id
from travel_eval.clock import parse_timestamp


ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "output" / "pdf" / "synthetic_direct_eticket.pdf"


def test_poll_identity_preserves_postgres_subsecond_precision() -> None:
    value = datetime(2026, 8, 26, 21, 54, 45, 123456, tzinfo=timezone.utc)

    assert format_timestamp(value) == "2026-08-26T21:54:45Z"
    assert format_poll_identity(value) == "2026-08-26T21:54:45.123456Z"
    assert parse_timestamp(format_poll_identity(value)) == value


class MemoryDocumentStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls = 0

    def ensure_bucket(self) -> None:
        return None

    def put_pdf(
        self, document_bytes: bytes, metadata: DocumentMetadata
    ) -> DocumentObjectRef:
        self.put_calls += 1
        key = f"trips/{metadata.trip_id}/documents/{metadata.sha256}.pdf"
        self.objects[key] = document_bytes
        return DocumentObjectRef(
            bucket="test-itineraries",
            key=key,
            sha256=metadata.sha256,
            etag="fixture-etag",
        )

    def verify(self, document: DocumentObjectRef) -> bool:
        content = self.objects.get(document.key)
        return content is not None and hashlib.sha256(content).hexdigest() == document.sha256


class ParsingGateway:
    def __init__(self, outcome: ParseOutcome | None = None) -> None:
        self.calls = 0
        self.outcome = outcome

    async def parse(
        self, document_bytes: bytes, metadata: DocumentMetadata
    ) -> ParseOutcome:
        self.calls += 1
        if self.outcome is not None:
            return self.outcome
        return parse_extracted_text(extract_pdf_text(document_bytes), metadata)


class ReviewGateway:
    async def parse(
        self, document_bytes: bytes, metadata: DocumentMetadata
    ) -> ParseOutcome:
        del document_bytes
        return ParseOutcome(
            status="review_required",
            document=metadata,
            review={
                "review_required": True,
                "reason_codes": ["LOW_OCR_CONFIDENCE_FLIGHT_NUMBER"],
            },
            orchestration={"framework": "crewai-flow"},
        )


class SequenceMonitoringGateway:
    def __init__(self) -> None:
        self.calls: list[MonitoringPollRequest] = []
        self.trace_ids: list[str | None] = []

    async def poll(self, request: MonitoringPollRequest) -> MonitoringPollOutcome:
        self.calls.append(request)
        self.trace_ids.append(current_trace_id())
        if len(self.calls) == 1:
            return MonitoringPollOutcome(
                status="baseline_stored",
                request=request,
                orchestration={
                    "notification_action": {"status": "not_required"},
                    "search_action": {"status": "not_required"},
                },
            )
        return MonitoringPollOutcome(
            status="candidate_evaluated",
            request=request,
            candidate={"category": "CANCELLATION"},
            decision={"verdict": "NOTIFY_AND_SEARCH"},
            notification={"notification_id": "notification-unit-001"},
            search={"search_id": "search-unit-001"},
            orchestration={
                "notification_action": {"status": "delivered"},
                "search_action": {"status": "completed"},
            },
        )


class MemoryTripStore:
    def __init__(self) -> None:
        self.trips: dict[str, StoredTripView] = {}
        self.scheduled: dict[tuple[str, str], dict[str, Any]] = {}
        self.trace_contexts: dict[str, dict[str, str]] = {}
        self.contacts: dict[str, NotificationRecipient] = {}

    def ensure_schema(self) -> None:
        return None

    def get_trip(self, trip_id: str) -> StoredTripView | None:
        trip = self.trips.get(trip_id)
        return trip.model_copy(deep=True) if trip else None

    def get_notification_recipient(
        self, trip_id: str
    ) -> NotificationRecipient | None:
        recipient = self.contacts.get(trip_id)
        return recipient.model_copy(deep=True) if recipient else None

    def save_parsed_trip(
        self,
        itinerary,
        document,
        *,
        created_at: datetime,
        notification_preference=None,
    ) -> bool:
        if itinerary.trip_id in self.trips:
            return False
        legs = []
        for leg in itinerary.legs:
            view = StoredLegView(
                leg_id=leg.leg_id,
                flight_iata=leg.flight_number,
                origin=leg.origin,
                destination=leg.destination,
                monitoring_status="active",
                next_poll_at=format_timestamp(created_at),
                poll_count=0,
            )
            legs.append(view)
            self.scheduled[(itinerary.trip_id, leg.leg_id)] = {
                "departure": leg.scheduled_departure_at,
                "arrival": leg.scheduled_arrival_at,
                "leased_until": None,
            }
        now = format_timestamp(created_at)
        self.trips[itinerary.trip_id] = StoredTripView(
            trip_id=itinerary.trip_id,
            traveler_ref=itinerary.traveler_ref,
            status="active",
            document=document,
            itinerary=itinerary,
            legs=legs,
            created_at=now,
            updated_at=now,
        )
        if notification_preference is not None:
            self.contacts[itinerary.trip_id] = NotificationRecipient(
                trip_id=itinerary.trip_id,
                recipient_ref=f"traveler:{itinerary.trip_id}",
                phone_e164=notification_preference.phone_e164,
                consent_granted_at=notification_preference.consent_granted_at,
            )
        return True

    def save_review_trip(
        self,
        *,
        trip_id,
        traveler_ref,
        document,
        review,
        created_at,
        notification_preference=None,
    ) -> bool:
        if trip_id in self.trips:
            return False
        now = format_timestamp(created_at)
        self.trips[trip_id] = StoredTripView(
            trip_id=trip_id,
            traveler_ref=traveler_ref,
            status="review_required",
            document=document,
            review=copy.deepcopy(review),
            created_at=now,
            updated_at=now,
        )
        if notification_preference is not None:
            self.contacts[trip_id] = NotificationRecipient(
                trip_id=trip_id,
                recipient_ref=f"traveler:{trip_id}",
                phone_e164=notification_preference.phone_e164,
                consent_granted_at=notification_preference.consent_granted_at,
            )
        return True

    def put_trace_context(self, trip_id, trace_headers):
        self.trace_contexts[trip_id] = dict(trace_headers)

    def claim_due_legs(self, *, now, maximum_legs, lease_seconds):
        claimed = []
        for trip_id in sorted(self.trips):
            trip = self.trips[trip_id]
            for leg in trip.legs:
                state = self.scheduled[(trip_id, leg.leg_id)]
                leased_until = state["leased_until"]
                if (
                    leg.monitoring_status != "active"
                    or leg.next_poll_at is None
                    or parse_timestamp(leg.next_poll_at) > now
                    or (leased_until is not None and leased_until > now)
                ):
                    continue
                state["leased_until"] = now + timedelta(seconds=lease_seconds)
                claimed.append(
                    ScheduledLeg(
                        trip_id=trip_id,
                        leg_id=leg.leg_id,
                        flight_iata=leg.flight_iata,
                        flight_date=parse_timestamp(state["departure"]).date().isoformat(),
                        scheduled_departure_at=state["departure"],
                        scheduled_arrival_at=state["arrival"],
                        due_at=leg.next_poll_at,
                        replay_key=f"scheduled:{trip_id}:{leg.leg_id}",
                        trace_headers=self.trace_contexts.get(trip_id, {}),
                    )
                )
                if len(claimed) >= maximum_legs:
                    return claimed
        return claimed

    def complete_poll(self, scheduled_leg, outcome, *, completed_at):
        trip = self.trips[scheduled_leg.trip_id]
        leg = next(item for item in trip.legs if item.leg_id == scheduled_leg.leg_id)
        status, next_at = next_poll_time(
            scheduled_departure_at=scheduled_leg.scheduled_departure_at,
            scheduled_arrival_at=scheduled_leg.scheduled_arrival_at,
            completed_at=completed_at,
        )
        leg.monitoring_status = status
        leg.next_poll_at = format_timestamp(next_at) if next_at else None
        leg.last_poll_at = format_timestamp(completed_at)
        leg.poll_count += 1
        leg.last_poll_status = outcome["status"]
        self.scheduled[(scheduled_leg.trip_id, scheduled_leg.leg_id)][
            "leased_until"
        ] = None
        trip.updated_at = format_timestamp(completed_at)

    def fail_poll(self, scheduled_leg, *, error_code, failed_at):
        del error_code
        trip = self.trips[scheduled_leg.trip_id]
        leg = next(item for item in trip.legs if item.leg_id == scheduled_leg.leg_id)
        leg.next_poll_at = format_timestamp(failed_at + timedelta(minutes=5))
        leg.last_poll_at = format_timestamp(failed_at)
        leg.last_poll_status = "poll_failed"
        self.scheduled[(scheduled_leg.trip_id, scheduled_leg.leg_id)][
            "leased_until"
        ] = None


def metadata(trip_id: str, content: bytes) -> DocumentMetadata:
    return DocumentMetadata(
        trip_id=trip_id,
        traveler_ref="traveler-synthetic-001",
        fixture_id="doc-v7-unit",
        filename=PDF.name,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def test_activation_is_content_addressed_and_idempotent() -> None:
    content = PDF.read_bytes()
    documents = MemoryDocumentStore()
    trips = MemoryTripStore()
    parser = ParsingGateway()
    monitoring = SequenceMonitoringGateway()
    orchestrator = TripOrchestrator(
        document_agent=parser,
        monitoring_agent=monitoring,
        document_store=documents,
        trip_store=trips,
    )
    at = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)

    first = asyncio.run(
        orchestrator.activate_trip(content, metadata("trip-v7-unit", content), activated_at=at)
    )
    second = asyncio.run(
        orchestrator.activate_trip(content, metadata("trip-v7-unit", content), activated_at=at)
    )

    assert first.status == "activated"
    assert first.active_leg_count == 1
    assert first.document.key.endswith(f"/{first.document.sha256}.pdf")
    assert second.status == "already_active"
    assert second.idempotent_replay is True
    assert parser.calls == 1
    assert documents.put_calls == 1
    assert asyncio.run(orchestrator.verify_document(first.trip_id)).stored is True


def test_same_trip_id_with_different_document_is_rejected() -> None:
    content = PDF.read_bytes()
    orchestrator = TripOrchestrator(
        document_agent=ParsingGateway(),
        monitoring_agent=SequenceMonitoringGateway(),
        document_store=MemoryDocumentStore(),
        trip_store=MemoryTripStore(),
    )
    asyncio.run(
        orchestrator.activate_trip(content, metadata("trip-v7-conflict", content))
    )
    changed = content + b"changed"

    with pytest.raises(TripDocumentConflictError):
        asyncio.run(
            orchestrator.activate_trip(
                changed, metadata("trip-v7-conflict", changed)
            )
        )


def test_consented_sms_contact_is_private_and_idempotent() -> None:
    content = PDF.read_bytes()
    trips = MemoryTripStore()
    orchestrator = TripOrchestrator(
        document_agent=ParsingGateway(),
        monitoring_agent=SequenceMonitoringGateway(),
        document_store=MemoryDocumentStore(),
        trip_store=trips,
    )
    preference = SmsNotificationPreference(
        phone_e164="+447700900123",
        consent_granted_at="2026-09-15T06:00:00Z",
    )

    first = asyncio.run(
        orchestrator.activate_trip(
            content,
            metadata("trip-v7-sms", content),
            notification_preference=preference,
        )
    )
    second = asyncio.run(
        orchestrator.activate_trip(
            content,
            metadata("trip-v7-sms", content),
            notification_preference=preference,
        )
    )
    recipient = asyncio.run(
        orchestrator.get_notification_recipient("trip-v7-sms")
    )

    assert first.status == "activated"
    assert second.status == "already_active"
    assert recipient is not None
    assert recipient.phone_e164 == "+447700900123"
    assert "phone" not in first.model_dump_json()


def test_review_required_document_is_stored_but_never_scheduled() -> None:
    content = PDF.read_bytes()
    trips = MemoryTripStore()
    orchestrator = TripOrchestrator(
        document_agent=ReviewGateway(),
        monitoring_agent=SequenceMonitoringGateway(),
        document_store=MemoryDocumentStore(),
        trip_store=trips,
    )

    activation = asyncio.run(
        orchestrator.activate_trip(content, metadata("trip-v7-review", content))
    )
    tick = asyncio.run(
        orchestrator.tick(SchedulerTickRequest(now="2026-09-15T06:00:00Z"))
    )

    assert activation.status == "review_required"
    assert activation.trip_status == "review_required"
    assert activation.active_leg_count == 0
    assert tick.claimed_count == 0


def test_activation_trace_context_is_restored_for_later_scheduled_poll() -> None:
    content = PDF.read_bytes()
    trips = MemoryTripStore()
    monitoring = SequenceMonitoringGateway()
    orchestrator = TripOrchestrator(
        document_agent=ParsingGateway(),
        monitoring_agent=monitoring,
        document_store=MemoryDocumentStore(),
        trip_store=trips,
    )
    span_context = SpanContext(
        trace_id=int("99999999999999999999999999999999", 16),
        span_id=int("2222222222222222", 16),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    token = otel_context.attach(
        trace.set_span_in_context(NonRecordingSpan(span_context))
    )
    try:
        asyncio.run(
            orchestrator.activate_trip(
                content,
                metadata("trip-v10-trace", content),
                activated_at=datetime(2026, 9, 15, 6, tzinfo=timezone.utc),
            )
        )
    finally:
        otel_context.detach(token)

    assert trips.trace_contexts["trip-v10-trace"]["traceparent"].startswith(
        "00-99999999999999999999999999999999-"
    )
    asyncio.run(
        orchestrator.tick(SchedulerTickRequest(now="2026-09-15T06:00:00Z"))
    )
    assert monitoring.trace_ids == ["99999999999999999999999999999999"]


def test_virtual_scheduler_survives_restart_without_duplicate_poll() -> None:
    content = PDF.read_bytes()
    documents = MemoryDocumentStore()
    trips = MemoryTripStore()
    monitoring = SequenceMonitoringGateway()
    first_process = TripOrchestrator(
        document_agent=ParsingGateway(),
        monitoring_agent=monitoring,
        document_store=documents,
        trip_store=trips,
    )
    at = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)
    asyncio.run(
        first_process.activate_trip(
            content, metadata("trip-v7-restart", content), activated_at=at
        )
    )

    baseline = asyncio.run(
        first_process.tick(SchedulerTickRequest(now="2026-09-15T06:00:00Z"))
    )
    restarted_process = TripOrchestrator(
        document_agent=ParsingGateway(),
        monitoring_agent=monitoring,
        document_store=documents,
        trip_store=trips,
    )
    duplicate = asyncio.run(
        restarted_process.tick(SchedulerTickRequest(now="2026-09-15T06:00:00Z"))
    )
    cancellation = asyncio.run(
        restarted_process.tick(SchedulerTickRequest(now="2026-09-15T06:10:00Z"))
    )

    assert baseline.completed_count == 1
    assert duplicate.claimed_count == 0
    assert cancellation.completed_count == 1
    assert cancellation.results[0].verdict == "NOTIFY_AND_SEARCH"
    assert cancellation.results[0].notification_status == "delivered"
    assert cancellation.results[0].search_status == "completed"
    assert len(monitoring.calls) == 2
    assert monitoring.calls[0].replay_key == monitoring.calls[1].replay_key
    stored = trips.get_trip("trip-v7-restart")
    assert stored is not None
    assert stored.legs[0].poll_count == 2
    assert stored.legs[0].next_poll_at == "2026-09-15T06:20:00Z"


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}

    def head_bucket(self, *, Bucket):
        return {"Bucket": Bucket}

    def create_bucket(self, **kwargs):
        return kwargs

    def head_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError

        try:
            return self.objects[(Bucket, Key)]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "missing"}},
                "HeadObject",
            ) from None

    def put_object(self, *, Bucket, Key, Body, ContentType, Metadata):
        assert ContentType == "application/pdf"
        self.objects[(Bucket, Key)] = {
            "Body": Body,
            "Metadata": Metadata,
            "ETag": '"fake-etag"',
        }
        return {"ETag": '"fake-etag"'}


def test_s3_document_store_writes_once_and_verifies_checksum_metadata() -> None:
    content = PDF.read_bytes()
    client = FakeS3Client()
    store = S3DocumentStore(bucket="test-bucket", client=client, region="eu-west-2")
    document_metadata = metadata("trip-v7-s3", content)

    first = store.put_pdf(content, document_metadata)
    second = store.put_pdf(content, document_metadata)

    assert first == second
    assert first.etag == "fake-etag"
    assert store.verify(first) is True
    assert len(client.objects) == 1
