from __future__ import annotations

import asyncio
import hashlib
import os

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from flight_agent.a2a_client import A2ADocumentAgentClient, DocumentAgentGateway
from flight_agent.contracts import DocumentMetadata
from flight_agent.document_store import DocumentStore, S3DocumentStore
from flight_agent.monitoring_a2a_client import (
    A2AMonitoringAgentClient,
    MonitoringAgentGateway,
)
from flight_agent.monitoring_contracts import MonitoringPollRequest
from flight_agent.trip_contracts import (
    DocumentStorageStatus,
    SchedulerPollResult,
    SchedulerTickOutcome,
    SchedulerTickRequest,
    StoredTripView,
    TripActivationOutcome,
)
from flight_agent.trip_store import PostgresTripStore, TripStore, format_timestamp
from flight_agent.telemetry import (
    extracted_trace_context,
    hash_reference,
    install_telemetry_routes,
    trace_headers,
    trace_operation,
)
from travel_eval.clock import parse_timestamp


MAX_PDF_BYTES = 5 * 1024 * 1024


class TripDocumentConflictError(RuntimeError):
    """The trip ID already belongs to different immutable source evidence."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _activation_from_view(
    trip: StoredTripView, *, idempotent_replay: bool
) -> TripActivationOutcome:
    next_times = [leg.next_poll_at for leg in trip.legs if leg.next_poll_at]
    return TripActivationOutcome(
        status="already_active" if idempotent_replay else (
            "review_required" if trip.status == "review_required" else "activated"
        ),
        trip_id=trip.trip_id,
        trip_status=(
            "review_required" if trip.status == "review_required" else "active"
        ),
        parse_status=(
            "review_required" if trip.status == "review_required" else "parsed"
        ),
        document=trip.document,
        itinerary=trip.itinerary,
        review=trip.review,
        active_leg_count=sum(
            leg.monitoring_status == "active" for leg in trip.legs
        ),
        next_poll_at=min(next_times) if next_times else None,
        idempotent_replay=idempotent_replay,
    )


class TripOrchestrator:
    """Coordinates one-time document activation and repeatable due-leg polling."""

    def __init__(
        self,
        *,
        document_agent: DocumentAgentGateway,
        monitoring_agent: MonitoringAgentGateway,
        document_store: DocumentStore,
        trip_store: TripStore,
        lease_seconds: int = 120,
    ) -> None:
        self._document_agent = document_agent
        self._monitoring_agent = monitoring_agent
        self._document_store = document_store
        self._trip_store = trip_store
        self._lease_seconds = lease_seconds

    async def activate_trip(
        self,
        document_bytes: bytes,
        metadata: DocumentMetadata,
        *,
        activated_at: datetime | None = None,
    ) -> TripActivationOutcome:
        activation_trace_headers = trace_headers()
        existing = await asyncio.to_thread(self._trip_store.get_trip, metadata.trip_id)
        if existing is not None:
            if (
                existing.document.sha256 != metadata.sha256
                or existing.traveler_ref != metadata.traveler_ref
            ):
                raise TripDocumentConflictError(
                    "Trip ID is already registered to different source evidence"
                )
            return _activation_from_view(existing, idempotent_replay=True)

        document = await asyncio.to_thread(
            self._document_store.put_pdf, document_bytes, metadata
        )
        parsed = await self._document_agent.parse(document_bytes, metadata)
        if parsed.document != metadata:
            raise RuntimeError("Document Agent returned mismatched source authority")
        if parsed.itinerary is not None and (
            parsed.itinerary.trip_id != metadata.trip_id
            or parsed.itinerary.traveler_ref != metadata.traveler_ref
        ):
            raise RuntimeError("Document Agent returned mismatched trip authority")
        now = activated_at or _now_utc()
        if parsed.status == "parsed" and parsed.itinerary is not None:
            inserted = await asyncio.to_thread(
                self._trip_store.save_parsed_trip,
                parsed.itinerary,
                document,
                created_at=now,
            )
        else:
            inserted = await asyncio.to_thread(
                self._trip_store.save_review_trip,
                trip_id=metadata.trip_id,
                traveler_ref=metadata.traveler_ref,
                document=document,
                review=parsed.review or {"reason_codes": ["PARSE_REVIEW_REQUIRED"]},
                created_at=now,
            )

        trace_writer = getattr(self._trip_store, "put_trace_context", None)
        if inserted and activation_trace_headers and callable(trace_writer):
            await asyncio.to_thread(
                trace_writer,
                metadata.trip_id,
                activation_trace_headers,
            )

        stored = await asyncio.to_thread(self._trip_store.get_trip, metadata.trip_id)
        if stored is None:
            raise RuntimeError("Trip persistence did not return the stored activation")
        if stored.document.sha256 != metadata.sha256:
            raise TripDocumentConflictError(
                "Concurrent activation registered different source evidence"
            )
        return _activation_from_view(stored, idempotent_replay=not inserted)

    async def get_trip(self, trip_id: str) -> StoredTripView | None:
        return await asyncio.to_thread(self._trip_store.get_trip, trip_id)

    async def verify_document(self, trip_id: str) -> DocumentStorageStatus | None:
        trip = await self.get_trip(trip_id)
        if trip is None:
            return None
        stored = await asyncio.to_thread(
            self._document_store.verify, trip.document
        )
        return DocumentStorageStatus(
            trip_id=trip_id, stored=stored, document=trip.document
        )

    async def tick(self, request: SchedulerTickRequest) -> SchedulerTickOutcome:
        now = parse_timestamp(request.now)
        due = await asyncio.to_thread(
            self._trip_store.claim_due_legs,
            now=now,
            maximum_legs=request.maximum_legs,
            lease_seconds=self._lease_seconds,
        )
        results: list[SchedulerPollResult] = []
        for leg in due:
            try:
                with extracted_trace_context(leg.trace_headers):
                    with trace_operation(
                        "scheduler.poll_leg",
                        service_name="trip-orchestrator",
                        kind="chain",
                        attributes={
                            "travel.trip_ref": hash_reference(leg.trip_id),
                            "travel.leg_ref": hash_reference(leg.leg_id),
                        },
                    ):
                        outcome = await self._monitoring_agent.poll(
                            MonitoringPollRequest(
                                trip_id=leg.trip_id,
                                leg_id=leg.leg_id,
                                flight_iata=leg.flight_iata,
                                flight_date=leg.flight_date,
                                replay_key=leg.replay_key,
                            )
                        )
                await asyncio.to_thread(
                    self._trip_store.complete_poll,
                    leg,
                    outcome.model_dump(mode="json", exclude_none=True),
                    completed_at=now,
                )
                results.append(
                    SchedulerPollResult(
                        trip_id=leg.trip_id,
                        leg_id=leg.leg_id,
                        poll_key=leg.poll_key,
                        status="completed",
                        monitoring_status=outcome.status,
                        category=(
                            str(outcome.candidate.get("category"))
                            if outcome.candidate
                            else None
                        ),
                        verdict=(
                            str(outcome.decision.get("verdict"))
                            if outcome.decision
                            else None
                        ),
                        notification_status=str(
                            outcome.orchestration.get("notification_action", {}).get(
                                "status", "not_required"
                            )
                        ),
                        search_status=str(
                            outcome.orchestration.get("search_action", {}).get(
                                "status", "not_required"
                            )
                        ),
                        notification_id=(
                            str(outcome.notification.get("notification_id"))
                            if outcome.notification
                            else None
                        ),
                        search_id=(
                            str(outcome.search.get("search_id"))
                            if outcome.search
                            else None
                        ),
                    )
                )
            except Exception:
                await asyncio.to_thread(
                    self._trip_store.fail_poll,
                    leg,
                    error_code="MONITORING_AGENT_FAILED",
                    failed_at=now,
                )
                results.append(
                    SchedulerPollResult(
                        trip_id=leg.trip_id,
                        leg_id=leg.leg_id,
                        poll_key=leg.poll_key,
                        status="failed",
                        error_code="MONITORING_AGENT_FAILED",
                    )
                )
        completed = sum(result.status == "completed" for result in results)
        return SchedulerTickOutcome(
            requested_at=format_timestamp(now),
            claimed_count=len(due),
            completed_count=completed,
            failed_count=len(results) - completed,
            results=results,
        )


def create_trip_orchestrator_app(
    *,
    orchestrator: TripOrchestrator | None = None,
    trip_store: TripStore | None = None,
    document_store: DocumentStore | None = None,
    scheduler_control_enabled: bool | None = None,
    background_scheduler_enabled: bool | None = None,
) -> FastAPI:
    resolved_trip_store = trip_store or PostgresTripStore.from_environment()
    resolved_document_store = document_store or S3DocumentStore.from_environment()
    resolved_orchestrator = orchestrator or TripOrchestrator(
        document_agent=A2ADocumentAgentClient(
            os.getenv("DOCUMENT_AGENT_URL", "http://127.0.0.1:8001")
        ),
        monitoring_agent=A2AMonitoringAgentClient(
            os.getenv("MONITOR_AGENT_URL", "http://127.0.0.1:8004")
        ),
        document_store=resolved_document_store,
        trip_store=resolved_trip_store,
        lease_seconds=int(os.getenv("SCHEDULER_LEASE_SECONDS", "120")),
    )
    control_enabled = (
        scheduler_control_enabled
        if scheduler_control_enabled is not None
        else os.getenv("SCHEDULER_CONTROL_ENABLED", "false").lower() == "true"
    )
    background_enabled = (
        background_scheduler_enabled
        if background_scheduler_enabled is not None
        else os.getenv("SCHEDULER_BACKGROUND_ENABLED", "true").lower() == "true"
    )
    interval_seconds = max(
        5, int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "30"))
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(resolved_trip_store.ensure_schema)
        await asyncio.to_thread(resolved_document_store.ensure_bucket)
        app.state.ready = True
        stop = asyncio.Event()

        async def schedule_loop() -> None:
            while not stop.is_set():
                try:
                    await resolved_orchestrator.tick(
                        SchedulerTickRequest(
                            now=format_timestamp(_now_utc()), maximum_legs=20
                        )
                    )
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
                except TimeoutError:
                    continue

        task = asyncio.create_task(schedule_loop()) if background_enabled else None
        try:
            yield
        finally:
            app.state.ready = False
            stop.set()
            if task is not None:
                await task

    app = FastAPI(
        title="Travel Trip Activation and Scheduling Orchestrator",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.ready = False
    install_telemetry_routes(app, service_name="trip-orchestrator")

    @app.get("/health/live", tags=["health"])
    async def health() -> dict[str, str]:
        if not app.state.ready:
            raise HTTPException(status_code=503, detail="Orchestrator is starting")
        return {"status": "ok"}

    @app.post("/v1/trips/activate", response_model=TripActivationOutcome)
    async def activate_trip(
        file: UploadFile = File(...),
        trip_id: str = Form(...),
        traveler_ref: str = Form(...),
        fixture_id: str = Form(...),
    ) -> TripActivationOutcome:
        document_bytes = await file.read(MAX_PDF_BYTES + 1)
        if len(document_bytes) > MAX_PDF_BYTES:
            raise HTTPException(status_code=413, detail="PDF exceeds 5 MiB limit")
        if file.content_type != "application/pdf" or not document_bytes.startswith(
            b"%PDF-"
        ):
            raise HTTPException(status_code=415, detail="A PDF upload is required")
        metadata = DocumentMetadata(
            trip_id=trip_id,
            traveler_ref=traveler_ref,
            fixture_id=fixture_id,
            filename=file.filename or "itinerary.pdf",
            sha256=hashlib.sha256(document_bytes).hexdigest(),
        )
        try:
            return await resolved_orchestrator.activate_trip(document_bytes, metadata)
        except TripDocumentConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/v1/trips/{trip_id}", response_model=StoredTripView)
    async def get_trip(trip_id: str) -> StoredTripView:
        trip = await resolved_orchestrator.get_trip(trip_id)
        if trip is None:
            raise HTTPException(status_code=404, detail="Trip not found")
        return trip

    @app.get(
        "/v1/trips/{trip_id}/document-status",
        response_model=DocumentStorageStatus,
    )
    async def document_status(trip_id: str) -> DocumentStorageStatus:
        status = await resolved_orchestrator.verify_document(trip_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Trip not found")
        return status

    @app.post("/v1/scheduler/tick", response_model=SchedulerTickOutcome)
    async def scheduler_tick(request: SchedulerTickRequest) -> SchedulerTickOutcome:
        if not control_enabled:
            raise HTTPException(status_code=404, detail="Scheduler control is disabled")
        return await resolved_orchestrator.tick(request)

    return app


app = create_trip_orchestrator_app()
