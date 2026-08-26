from __future__ import annotations

import os
import tempfile

from pathlib import Path
from typing import Any, ClassVar

if os.name == "nt":
    os.environ["LOCALAPPDATA"] = str(
        Path(tempfile.gettempdir()) / "flight-monitor-runtime"
    )
os.environ.setdefault(
    "CREWAI_STORAGE_DIR", str(Path(tempfile.gettempdir()) / "flight-monitor-crewai")
)
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_DISABLE_TRACKING", "true")
os.environ.setdefault("CREWAI_DISABLE_VERSION_CHECK", "true")

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel, Field, PrivateAttr

from flight_agent.flight_status_mcp_client import (
    FlightStatusGateway,
    StreamableHttpFlightStatusMcpClient,
)
from flight_agent.monitoring_contracts import (
    MonitoringPollOutcome,
    MonitoringPollRequest,
)
from flight_agent.monitoring_events import CandidatePublisher, NatsCandidatePublisher
from flight_agent.monitoring_store import (
    DynamoMonitoringStateStore,
    MonitoringStore,
)
from travel_eval.clock import parse_timestamp
from travel_eval.engine import (
    candidate_score,
    changed_fields,
    classify_candidate,
    operational_delay,
)


def _event_id(prefix: str, request: MonitoringPollRequest, observation_id: str) -> str:
    """Scope provider observation ids to a trip leg for durable idempotency."""
    trip = request.trip_id.removeprefix("trip-")
    leg = request.leg_id.removeprefix("leg-")
    observation = observation_id.removeprefix("obs-")
    return f"{prefix}-{trip}-{leg}-{observation}"


class MonitoringState(BaseModel):
    request: dict[str, Any] = Field(default_factory=dict)
    provider_observation: dict[str, Any] = Field(default_factory=dict)
    fetch_error: str | None = None
    outcome: dict[str, Any] = Field(default_factory=dict)


class FlightMonitoringFlow(Flow[MonitoringState]):
    """CrewAI flow that polls MCP, diffs durable state, and publishes candidates."""

    tracing: bool | None = False
    suppress_flow_events: bool = True
    _skip_auto_memory: ClassVar[bool] = True

    _flight_status: FlightStatusGateway = PrivateAttr()
    _store: MonitoringStore = PrivateAttr()
    _publisher: CandidatePublisher = PrivateAttr()

    def __init__(
        self,
        *,
        flight_status: FlightStatusGateway,
        store: MonitoringStore,
        publisher: CandidatePublisher,
    ) -> None:
        super().__init__()
        self._flight_status = flight_status
        self._store = store
        self._publisher = publisher

    @start()
    def fetch_status(self) -> dict[str, Any]:
        request = MonitoringPollRequest.model_validate(self.state.request)
        try:
            observation = self._flight_status.get_flight_status(
                flight_iata=request.flight_iata,
                flight_date=request.flight_date,
                replay_key=request.replay_key,
            )
        except Exception:
            self.state.fetch_error = "FLIGHT_STATUS_MCP_FAILED"
            return {}
        self.state.provider_observation = observation.model_dump(mode="json")
        return self.state.provider_observation

    @listen(fetch_status)
    def evaluate_delta(self, provider_observation: dict[str, Any]) -> dict[str, Any]:
        request = MonitoringPollRequest.model_validate(self.state.request)
        if self.state.fetch_error:
            outcome = MonitoringPollOutcome(
                status="poll_failed",
                request=request,
                error_code=self.state.fetch_error,
                orchestration={
                    "framework": "crewai-flow",
                    "steps": ["mcp.get_flight_status", "fail_closed"],
                    "mcp_calls": 1,
                    "candidate_events_published": 0,
                },
            )
            self.state.outcome = outcome.model_dump(mode="json", exclude_none=True)
            return self.state.outcome

        observation = {
            **provider_observation,
            "trip_id": request.trip_id,
            "leg_id": request.leg_id,
        }
        previous = self._store.get_last_observation(
            request.trip_id, request.leg_id
        )
        if previous is not None and parse_timestamp(
            observation["source_event_time"]
        ) <= parse_timestamp(previous["source_event_time"]):
            return self._finish_without_candidate(
                request,
                observation,
                status="stale_observation",
                persist=False,
            )
        if previous is None:
            return self._finish_without_candidate(
                request,
                observation,
                status="baseline_stored",
                persist=True,
            )

        fields = changed_fields(previous, observation)
        if not fields:
            return self._finish_without_candidate(
                request,
                observation,
                status="unchanged",
                persist=True,
            )

        delta_id = _event_id("delta", request, observation["observation_id"])
        candidate_id = _event_id("cand", request, observation["observation_id"])
        before_delay = operational_delay(previous)
        after_delay = operational_delay(observation)
        category = classify_candidate(observation, fields, None, None)
        delta = {
            "schema_version": "1.0.0",
            "delta_id": delta_id,
            "trip_id": request.trip_id,
            "leg_id": request.leg_id,
            "previous_observation_id": previous["observation_id"],
            "current_observation_id": observation["observation_id"],
            "observed_at": observation["observed_at"],
            "changed_fields": fields,
            "delay_minutes_before": before_delay,
            "delay_minutes_after": after_delay,
            "delay_change_minutes": after_delay - before_delay,
        }
        candidate = {
            "schema_version": "1.0.0",
            "candidate_id": candidate_id,
            "delta": delta,
            "trip_id": request.trip_id,
            "leg_id": request.leg_id,
            "observed_at": observation["observed_at"],
            "category": category,
            "delay_minutes": after_delay,
            "connection_buffer_minutes": None,
            "minimum_connection_minutes": None,
            "weather_risk_level": observation["weather"]["risk_level"],
            "confidence": observation["confidence"],
            "score": candidate_score(
                category, after_delay, observation["confidence"]
            ),
            "evidence_observation_ids": [
                previous["observation_id"],
                observation["observation_id"],
            ],
        }

        try:
            self._store.put_candidate(candidate)
            self._publisher.publish_candidate(candidate)
        except Exception:
            outcome = MonitoringPollOutcome(
                status="poll_failed",
                request=request,
                observation=observation,
                candidate=candidate,
                error_code="CANDIDATE_EVENT_PUBLISH_FAILED",
                orchestration={
                    "framework": "crewai-flow",
                    "steps": [
                        "mcp.get_flight_status",
                        "dynamodb.diff_last_status",
                        "nats.publish_failed",
                    ],
                    "mcp_calls": 1,
                    "candidate_events_published": 0,
                },
            )
            self.state.outcome = outcome.model_dump(mode="json", exclude_none=True)
            return self.state.outcome

        self._store.put_last_observation(
            request.trip_id, request.leg_id, observation
        )
        decision, confirmed_event = self._store.wait_for_decision(
            candidate["candidate_id"], timeout_seconds=5
        )
        status = "candidate_evaluated" if decision else "evaluation_pending"
        outcome = MonitoringPollOutcome(
            status=status,
            request=request,
            observation=observation,
            candidate=candidate,
            decision=decision,
            confirmed_event=confirmed_event,
            orchestration={
                "framework": "crewai-flow",
                "steps": [
                    "mcp.get_flight_status",
                    "dynamodb.diff_last_status",
                    "nats.publish_disruption_candidate",
                    "eval_agent.consume_candidate",
                ],
                "mcp_calls": 1,
                "candidate_events_published": 1,
            },
        )
        self.state.outcome = outcome.model_dump(mode="json", exclude_none=True)
        return self.state.outcome

    def _finish_without_candidate(
        self,
        request: MonitoringPollRequest,
        observation: dict[str, Any],
        *,
        status: str,
        persist: bool,
    ) -> dict[str, Any]:
        if persist:
            self._store.put_last_observation(
                request.trip_id, request.leg_id, observation
            )
        outcome = MonitoringPollOutcome(
            status=status,
            request=request,
            observation=observation,
            orchestration={
                "framework": "crewai-flow",
                "steps": [
                    "mcp.get_flight_status",
                    "dynamodb.diff_last_status",
                ],
                "mcp_calls": 1,
                "candidate_events_published": 0,
            },
        )
        self.state.outcome = outcome.model_dump(mode="json", exclude_none=True)
        return self.state.outcome


def run_monitoring_flow(
    request: MonitoringPollRequest,
    *,
    flight_status: FlightStatusGateway | None = None,
    store: MonitoringStore | None = None,
    publisher: CandidatePublisher | None = None,
) -> dict[str, Any]:
    resolved_store = store or DynamoMonitoringStateStore.from_environment()
    flow = FlightMonitoringFlow(
        flight_status=flight_status
        or StreamableHttpFlightStatusMcpClient(
            os.getenv("FLIGHT_STATUS_MCP_URL", "http://127.0.0.1:8003/mcp")
        ),
        store=resolved_store,
        publisher=publisher
        or NatsCandidatePublisher(os.getenv("NATS_URL", "nats://127.0.0.1:4222")),
    )
    result = flow.kickoff(inputs={"request": request.model_dump(mode="json")})
    if not isinstance(result, dict):
        raise TypeError("FlightMonitoringFlow must return a JSON object")
    return result
