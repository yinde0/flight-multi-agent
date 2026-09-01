from __future__ import annotations

import asyncio
import json
import os

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from flight_agent.eval_reasoning import (
    PROMPT_VERSION,
    EvalReasoner,
    advisory_record,
    candidate_trace_evidence,
    reasoner_from_environment,
)
from flight_agent.event_bus import connect_event_bus
from flight_agent.event_delivery import (
    EVAL_CONSUMER,
    EVENT_STREAM_NAME,
    DISRUPTION_CANDIDATE_SUBJECT,
    NOTIFICATION_CONSUMER,
    SEARCH_CONSUMER,
    confirmed_outbox,
    consume_event_trace,
    decode_envelope,
    ensure_event_stream,
    fallback_event_id,
    publish_durable_event,
    publish_pending_outbox,
    quarantine_message,
    retry_or_quarantine,
    subscribe_durable,
)
from flight_agent.monitoring_store import DynamoMonitoringStateStore, MonitoringStore
from flight_agent.telemetry import (
    configure_telemetry,
    hash_reference,
    install_telemetry_routes,
    traced,
)
from travel_eval.policy import PolicyState, SuppressionPolicy


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = (
    ROOT / "travel_eval" / "policies" / "suppression_policy.v1.json"
)


def load_policy() -> SuppressionPolicy:
    path = Path(os.getenv("SUPPRESSION_POLICY_PATH", str(DEFAULT_POLICY_PATH)))
    return SuppressionPolicy(json.loads(path.read_text(encoding="utf-8")))


def eval_agent_trace_input(
    candidate: dict[str, Any],
    store: MonitoringStore,
    policy: SuppressionPolicy,
) -> dict[str, Any]:
    return {
        "task": "Apply suppression policy and authorize or suppress traveler action.",
        "candidate": candidate_trace_evidence(candidate),
        "policy": {
            "policy_version": policy.version,
            "delay_thresholds": policy.delay_thresholds,
            "cooldown_minutes": policy.cooldown_minutes,
        },
        "candidate_ref": hash_reference(candidate.get("candidate_id", "")),
    }


def eval_agent_trace_output(
    result: tuple[dict[str, Any], dict[str, Any] | None, str, int | None, bool]
) -> dict[str, Any]:
    decision, confirmed_event, _episode_key, severity_band, duplicate = result
    return {
        "verdict": decision.get("verdict"),
        "reason_codes": decision.get("reason_codes", []),
        "policy_version": decision.get("policy_version"),
        "confidence": decision.get("confidence"),
        "severity_band": severity_band,
        "duplicate_candidate": duplicate,
        "disruption_confirmed_published": confirmed_event is not None,
    }


@traced(
    "agent.eval.apply_policy",
    service_name="eval-agent",
    attributes=lambda candidate, store, policy: {
        "travel.candidate_ref": hash_reference(candidate.get("candidate_id", "")),
        "travel.trip_ref": hash_reference(candidate.get("trip_id", "")),
    },
    result_outcome=lambda result: (
        "duplicate"
        if result[4]
        else str(result[0].get("verdict", "unknown")).lower()
    ),
    content_input=eval_agent_trace_input,
    content_output=eval_agent_trace_output,
)
def evaluate_candidate(
    candidate: dict[str, Any],
    store: MonitoringStore,
    policy: SuppressionPolicy,
) -> tuple[
    dict[str, Any],
    dict[str, Any] | None,
    str,
    int | None,
    bool,
]:
    existing = store.get_decision(candidate["candidate_id"])
    if existing is not None:
        return (
            existing,
            store.get_confirmed_event(candidate["candidate_id"]),
            "",
            None,
            True,
        )

    episode_key = (
        f"{candidate['trip_id']}:{candidate['leg_id']}:{candidate['category']}"
    )
    previous_band = store.get_policy_band(episode_key)
    state = PolicyState()
    if previous_band is not None:
        state.highest_notified_band[episode_key] = previous_band
    suffix = candidate["candidate_id"].removeprefix("cand-")
    decision = policy.evaluate(
        candidate,
        state,
        decision_id=f"decision-{suffix}",
    )
    confirmed_event = None
    if decision["verdict"] != "SUPPRESS":
        confirmed_event = {
            "schema_version": "1.0.0",
            "event_type": "disruption_confirmed",
            "candidate_id": candidate["candidate_id"],
            "decision_id": decision["decision_id"],
            "trip_id": candidate["trip_id"],
            "leg_id": candidate["leg_id"],
            "category": candidate["category"],
            "verdict": decision["verdict"],
            "reason_codes": decision["reason_codes"],
            "published_at": decision["decided_at"],
        }
    return (
        decision,
        confirmed_event,
        episode_key,
        state.highest_notified_band.get(episode_key),
        False,
    )


def commit_evaluation(
    *,
    candidate_id: str,
    decision: dict[str, Any],
    confirmed_event: dict[str, Any] | None,
    episode_key: str,
    notified_band: int | None,
    store: MonitoringStore,
    advisory: dict[str, Any] | None = None,
) -> None:
    atomic_commit = getattr(store, "commit_evaluation_with_outbox", None)
    if callable(atomic_commit):
        arguments = dict(
            candidate_id=candidate_id,
            decision=decision,
            confirmed_event=confirmed_event,
            episode_key=episode_key,
            notified_band=notified_band,
        )
        if advisory is not None:
            arguments["advisory"] = advisory
        atomic_commit(**arguments)
        return
    if confirmed_event is not None:
        store.put_confirmed_event(candidate_id, confirmed_event)
    if episode_key and notified_band is not None:
        store.put_policy_band(episode_key, notified_band)
    advisory_writer = getattr(store, "put_eval_advisory", None)
    if advisory is not None and callable(advisory_writer):
        advisory_writer(candidate_id, advisory)
    # Written last so the Monitoring Agent only observes a fully committed result.
    store.put_decision(candidate_id, decision)


async def connect_nats(url: str, *, timeout_seconds: float = 30.0):
    """Compatibility seam returning the configured NATS or SQS event bus."""

    return await connect_event_bus(
        nats_url=url,
        timeout_seconds=timeout_seconds,
    )


def create_eval_app(
    store: MonitoringStore | None = None,
    policy: SuppressionPolicy | None = None,
    reasoner: EvalReasoner | None = None,
) -> FastAPI:
    configure_telemetry("eval-agent")
    resolved_store = store or DynamoMonitoringStateStore.from_environment()
    resolved_policy = policy or load_policy()
    resolved_reasoner = reasoner or reasoner_from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if isinstance(resolved_store, DynamoMonitoringStateStore):
            await asyncio.to_thread(resolved_store.ensure_table)
        connection = await connect_nats(
            os.getenv("NATS_URL", "nats://127.0.0.1:4222")
        )
        jetstream = connection.jetstream()
        await ensure_event_stream(jetstream)
        stop = asyncio.Event()

        async def publish_confirmed(record: dict[str, Any]) -> None:
            await publish_durable_event(jetstream, record)

        async def drain_confirmed_outbox() -> None:
            interval = max(
                0.25, float(os.getenv("OUTBOX_RETRY_INTERVAL_SECONDS", "2"))
            )
            while not stop.is_set():
                try:
                    await publish_pending_outbox(
                        store=resolved_store,
                        event_type="disruption_confirmed",
                        publish=publish_confirmed,
                    )
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    continue

        async def process_candidate_message(message) -> None:
            envelope = None
            try:
                envelope = decode_envelope(
                    message, expected_type="disruption_candidate"
                )
                candidate = envelope.payload
                (
                    decision,
                    confirmed_event,
                    episode_key,
                    notified_band,
                    already_processed,
                ) = await asyncio.to_thread(
                    evaluate_candidate,
                    candidate,
                    resolved_store,
                    resolved_policy,
                )
                if already_processed:
                    await message.ack_sync(timeout=3)
                    return
                advisory = await asyncio.to_thread(
                    advisory_record,
                    resolved_reasoner,
                    candidate,
                    resolved_policy.policy,
                    decision,
                )
                await asyncio.to_thread(
                    commit_evaluation,
                    candidate_id=candidate["candidate_id"],
                    decision=decision,
                    confirmed_event=confirmed_event,
                    episode_key=episode_key,
                    notified_band=notified_band,
                    store=resolved_store,
                    advisory=advisory,
                )
                if confirmed_event is not None:
                    record = confirmed_outbox(confirmed_event)
                    try:
                        await publish_confirmed(record)
                        deleter = getattr(resolved_store, "delete_outbox", None)
                        if callable(deleter):
                            await asyncio.to_thread(
                                deleter,
                                "disruption_confirmed",
                                str(record["event_id"]),
                            )
                    except Exception:
                        # The atomic outbox remains for the background publisher.
                        pass
                await message.ack_sync(timeout=3)
            except Exception:
                payload = envelope.payload if envelope is not None else {}
                event_id = (
                    envelope.event_id
                    if envelope is not None
                    else fallback_event_id(message)
                )
                if envelope is None:
                    await quarantine_message(
                        message,
                        store=resolved_store,
                        consumer=EVAL_CONSUMER,
                        event_id=event_id,
                        payload=payload,
                        error_code="CANDIDATE_EVENT_INVALID",
                    )
                else:
                    await retry_or_quarantine(
                        message,
                        store=resolved_store,
                        consumer=EVAL_CONSUMER,
                        event_id=event_id,
                        payload=payload,
                        error_code="EVALUATION_FAILED",
                    )

        async def handle_candidate(message) -> None:
            with consume_event_trace(
                message,
                service_name="eval-agent",
                operation="messaging.consume.disruption_candidate",
            ):
                await process_candidate_message(message)

        subscription = await subscribe_durable(
            jetstream,
            subject=DISRUPTION_CANDIDATE_SUBJECT,
            durable_name=EVAL_CONSUMER,
            callback=handle_candidate,
        )
        outbox_task = asyncio.create_task(drain_confirmed_outbox())
        app.state.ready = True
        app.state.nats = connection
        app.state.jetstream = jetstream
        try:
            yield
        finally:
            app.state.ready = False
            stop.set()
            await outbox_task
            await subscription.unsubscribe()
            await connection.drain()

    app = FastAPI(
        title="Travel Disruption Eval Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.ready = False
    install_telemetry_routes(app, service_name="eval-agent")

    @app.get("/health/live", tags=["health"])
    async def health() -> dict[str, str]:
        if not app.state.ready:
            raise HTTPException(status_code=503, detail="Eval Agent is starting")
        return {"status": "ok"}

    @app.get("/v1/evaluation/status", tags=["evaluation"])
    async def evaluation_status() -> dict[str, Any]:
        return {
            "reasoning_mode": "shadow" if resolved_reasoner is not None else "off",
            "prompt_version": PROMPT_VERSION,
            "model": (
                resolved_reasoner.model_name
                if resolved_reasoner is not None
                else None
            ),
            "authoritative_source": "deterministic_policy",
        }

    def require_reliability_audit() -> None:
        if os.getenv("RELIABILITY_AUDIT_ENABLED", "false").lower() != "true":
            raise HTTPException(status_code=404, detail="Reliability audit is disabled")

    @app.get(
        "/v1/reliability/events/{candidate_id}", tags=["test-control"]
    )
    async def reliability_event(candidate_id: str) -> dict[str, Any]:
        require_reliability_audit()
        candidate_reader = getattr(resolved_store, "get_candidate", None)
        candidate = (
            await asyncio.to_thread(candidate_reader, candidate_id)
            if callable(candidate_reader)
            else None
        )
        decision = await asyncio.to_thread(
            resolved_store.get_decision, candidate_id
        )
        confirmed = await asyncio.to_thread(
            resolved_store.get_confirmed_event, candidate_id
        )
        advisory_reader = getattr(resolved_store, "get_eval_advisory", None)
        advisory = (
            await asyncio.to_thread(advisory_reader, candidate_id)
            if callable(advisory_reader)
            else None
        )
        decision_id = str(decision["decision_id"]) if decision else None
        notification = (
            await asyncio.to_thread(
                resolved_store.get_notification, decision_id
            )
            if decision_id
            else None
        )
        search = (
            await asyncio.to_thread(resolved_store.get_search, decision_id)
            if decision_id
            else None
        )
        counter = getattr(resolved_store, "outbox_count", None)
        dead_counter = getattr(resolved_store, "dead_letter_count", None)
        outboxes = {}
        dead_letters = {}
        if callable(counter):
            for event_type in (
                "disruption_candidate",
                "disruption_confirmed",
            ):
                outboxes[event_type] = await asyncio.to_thread(
                    counter, event_type
                )
        if callable(dead_counter):
            for consumer in (
                EVAL_CONSUMER,
                NOTIFICATION_CONSUMER,
                SEARCH_CONSUMER,
            ):
                dead_letters[consumer] = await asyncio.to_thread(
                    dead_counter, consumer
                )
        return {
            "candidate": candidate,
            "decision": decision,
            "confirmed_event": confirmed,
            "eval_advisory": advisory,
            "notification": notification,
            "search": search,
            "outbox_pending": outboxes,
            "dead_letter_counts": dead_letters,
        }

    @app.get("/v1/reliability/bus", tags=["test-control"])
    async def reliability_bus() -> dict[str, Any]:
        require_reliability_audit()
        stream = await app.state.jetstream.stream_info(EVENT_STREAM_NAME)
        consumers: dict[str, Any] = {}
        for name in (EVAL_CONSUMER, NOTIFICATION_CONSUMER, SEARCH_CONSUMER):
            try:
                info = await app.state.jetstream.consumer_info(
                    EVENT_STREAM_NAME, name
                )
                consumers[name] = {
                    "pending": int(info.num_pending or 0),
                    "ack_pending": int(info.num_ack_pending or 0),
                    "redelivered": int(info.num_redelivered or 0),
                }
            except Exception:
                consumers[name] = None
        return {
            "stream": EVENT_STREAM_NAME,
            "messages": stream.state.messages,
            "consumer_count": stream.state.consumer_count,
            "consumers": consumers,
        }

    @app.post(
        "/v1/reliability/events/{candidate_id}/redeliver",
        tags=["test-control"],
    )
    async def force_redelivery(
        candidate_id: str, request: dict[str, str]
    ) -> dict[str, str]:
        require_reliability_audit()
        delivery_id = str(request.get("delivery_id") or "").strip()
        if not delivery_id:
            raise HTTPException(status_code=422, detail="delivery_id is required")
        event = await asyncio.to_thread(
            resolved_store.get_confirmed_event, candidate_id
        )
        if event is None:
            raise HTTPException(status_code=404, detail="Confirmed event not found")
        record = confirmed_outbox(event)
        await publish_durable_event(
            app.state.jetstream,
            record,
            message_id=f"forced-redelivery:{delivery_id}",
        )
        return {
            "status": "published",
            "event_id": str(record["event_id"]),
        }

    return app


app = create_eval_app()
