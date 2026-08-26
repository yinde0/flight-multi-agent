from __future__ import annotations

import asyncio
import json
import os

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import nats

from fastapi import FastAPI, HTTPException

from flight_agent.monitoring_events import (
    DISRUPTION_CANDIDATE_SUBJECT,
    DISRUPTION_CONFIRMED_SUBJECT,
)
from flight_agent.monitoring_store import DynamoMonitoringStateStore, MonitoringStore
from travel_eval.policy import PolicyState, SuppressionPolicy


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = (
    ROOT / "travel_eval" / "policies" / "suppression_policy.v1.json"
)


def load_policy() -> SuppressionPolicy:
    path = Path(os.getenv("SUPPRESSION_POLICY_PATH", str(DEFAULT_POLICY_PATH)))
    return SuppressionPolicy(json.loads(path.read_text(encoding="utf-8")))


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
) -> None:
    if confirmed_event is not None:
        store.put_confirmed_event(candidate_id, confirmed_event)
    if episode_key and notified_band is not None:
        store.put_policy_band(episode_key, notified_band)
    # Written last so the Monitoring Agent only observes a fully committed result.
    store.put_decision(candidate_id, decision)


async def connect_nats(url: str, *, timeout_seconds: float = 30.0):
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        try:
            return await nats.connect(
                servers=[url],
                connect_timeout=2,
                max_reconnect_attempts=10,
            )
        except Exception:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.5)


def create_eval_app(
    store: MonitoringStore | None = None,
    policy: SuppressionPolicy | None = None,
) -> FastAPI:
    resolved_store = store or DynamoMonitoringStateStore.from_environment()
    resolved_policy = policy or load_policy()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if isinstance(resolved_store, DynamoMonitoringStateStore):
            await asyncio.to_thread(resolved_store.ensure_table)
        connection = await connect_nats(
            os.getenv("NATS_URL", "nats://127.0.0.1:4222")
        )

        async def handle_candidate(message) -> None:
            try:
                candidate = json.loads(message.data.decode("utf-8"))
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
                    return
                if confirmed_event is not None:
                    await connection.publish(
                        DISRUPTION_CONFIRMED_SUBJECT,
                        json.dumps(
                            confirmed_event, separators=(",", ":")
                        ).encode("utf-8"),
                    )
                    await connection.flush(timeout=3)
                await asyncio.to_thread(
                    commit_evaluation,
                    candidate_id=candidate["candidate_id"],
                    decision=decision,
                    confirmed_event=confirmed_event,
                    episode_key=episode_key,
                    notified_band=notified_band,
                    store=resolved_store,
                )
            except Exception:
                # The health endpoint remains alive; missing decisions surface as
                # evaluation_pending to the orchestrator instead of fabricated output.
                return

        subscription = await connection.subscribe(
            DISRUPTION_CANDIDATE_SUBJECT,
            queue="travel-eval-agent-v1",
            cb=handle_candidate,
        )
        await connection.flush(timeout=3)
        app.state.ready = True
        app.state.nats = connection
        try:
            yield
        finally:
            app.state.ready = False
            await subscription.unsubscribe()
            await connection.drain()

    app = FastAPI(
        title="Travel Disruption Eval Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.ready = False

    @app.get("/health/live", tags=["health"])
    async def health() -> dict[str, str]:
        if not app.state.ready:
            raise HTTPException(status_code=503, detail="Eval Agent is starting")
        return {"status": "ok"}

    return app


app = create_eval_app()
