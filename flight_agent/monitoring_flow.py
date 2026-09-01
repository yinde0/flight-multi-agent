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
from flight_agent.monitoring_contracts import MonitoringPollOutcome, MonitoringPollRequest
from flight_agent.monitoring_events import CandidatePublisher, NatsCandidatePublisher
from flight_agent.monitoring_store import DynamoMonitoringStateStore, MonitoringStore
from flight_agent.telemetry import hash_reference, traced
from flight_agent.weather import NeutralWeatherGateway
from flight_agent.weather_mcp_client import StreamableHttpWeatherMcpClient, WeatherGateway
from travel_eval.clock import parse_timestamp
from travel_eval.engine import (
    candidate_score,
    changed_fields,
    classify_candidate,
    operational_delay,
)


def _event_id(prefix: str, request: MonitoringPollRequest, evidence_id: str) -> str:
    """Scope provider evidence ids to a trip leg for durable idempotency."""
    trip = request.trip_id.removeprefix("trip-")
    leg = request.leg_id.removeprefix("leg-")
    evidence = evidence_id.removeprefix("obs-").removeprefix("weather-")
    return f"{prefix}-{trip}-{leg}-{evidence}"


def _weather_fields(
    previous: dict[str, Any] | None,
    current: dict[str, Any] | None,
) -> list[str]:
    if current is None:
        return []
    if previous is None:
        return ["weather.risk_level", "weather.alerts"]
    fields: list[str] = []
    for key in ("risk_level", "alerts"):
        if previous.get(key) != current.get(key):
            fields.append(f"weather.{key}")
    return fields


def _airport_from_flight(observation: dict[str, Any]) -> str:
    explicit = observation.get("departure_airport")
    if isinstance(explicit, str) and len(explicit) == 3:
        return explicit.upper()
    legacy_weather = observation.get("weather")
    if isinstance(legacy_weather, dict):
        legacy = legacy_weather.get("airport")
        if isinstance(legacy, str) and len(legacy) == 3:
            return legacy.upper()
    raise ValueError("Flight observation has no departure airport")


class MonitoringState(BaseModel):
    request: dict[str, Any] = Field(default_factory=dict)
    provider_observation: dict[str, Any] = Field(default_factory=dict)
    provider_weather: dict[str, Any] = Field(default_factory=dict)
    fetch_error: str | None = None
    weather_error: str | None = None
    outcome: dict[str, Any] = Field(default_factory=dict)


class FlightMonitoringFlow(Flow[MonitoringState]):
    """CrewAI flow that combines flight and weather MCP evidence statefully."""

    tracing: bool | None = False
    suppress_flow_events: bool = True
    _skip_auto_memory: ClassVar[bool] = True

    _flight_status: FlightStatusGateway = PrivateAttr()
    _weather: WeatherGateway = PrivateAttr()
    _store: MonitoringStore = PrivateAttr()
    _publisher: CandidatePublisher = PrivateAttr()

    def __init__(
        self,
        *,
        flight_status: FlightStatusGateway,
        weather: WeatherGateway,
        store: MonitoringStore,
        publisher: CandidatePublisher,
    ) -> None:
        super().__init__()
        self._flight_status = flight_status
        self._weather = weather
        self._store = store
        self._publisher = publisher

    @start()
    def fetch_evidence(self) -> dict[str, Any]:
        request = MonitoringPollRequest.model_validate(self.state.request)
        try:
            flight = self._flight_status.get_flight_status(
                flight_iata=request.flight_iata,
                flight_date=request.flight_date,
                replay_key=request.replay_key,
            )
        except Exception:
            self.state.fetch_error = "FLIGHT_STATUS_MCP_FAILED"
            return {}

        self.state.provider_observation = flight.model_dump(mode="json")
        try:
            weather = self._weather.get_airport_weather(
                airport=_airport_from_flight(self.state.provider_observation),
                target_at=flight.departure.scheduled_at,
                replay_key=request.replay_key,
            )
            self.state.provider_weather = weather.model_dump(mode="json")
        except Exception:
            # Weather is supporting evidence. Flight monitoring must continue.
            self.state.weather_error = "WEATHER_MCP_FAILED"
        return self.state.provider_observation

    @listen(fetch_evidence)
    def evaluate_delta(self, provider_observation: dict[str, Any]) -> dict[str, Any]:
        request = MonitoringPollRequest.model_validate(self.state.request)
        if self.state.fetch_error:
            return self._set_outcome(
                MonitoringPollOutcome(
                    status="poll_failed",
                    request=request,
                    error_code=self.state.fetch_error,
                    orchestration={
                        "framework": "crewai-flow",
                        "steps": ["mcp.get_flight_status", "fail_closed"],
                        "mcp_calls": 1,
                        "weather_evidence": {"status": "not_requested"},
                        "notification_action": {"status": "not_required"},
                        "search_action": {"status": "not_required"},
                        "candidate_events_published": 0,
                    },
                )
            )

        flight = {
            **provider_observation,
            "trip_id": request.trip_id,
            "leg_id": request.leg_id,
            "flight_iata": request.flight_iata,
            "flight_date": request.flight_date,
        }
        # The flight provider's old weather field is compatibility input only.
        # Weather evidence must come through the dedicated weather MCP now.
        flight.pop("weather", None)
        current_weather = self.state.provider_weather or None
        previous_flight = self._store.get_last_observation(
            request.trip_id, request.leg_id
        )
        previous_weather = self._store.get_last_weather(request.trip_id, request.leg_id)

        weather_meta = {
            "status": "available" if current_weather else "unavailable",
            **(
                {"error_code": self.state.weather_error}
                if self.state.weather_error
                else {}
            ),
        }
        combined_observation = {**flight, "weather": current_weather}

        if previous_flight is None:
            self._persist(request, flight, current_weather)
            return self._set_outcome(
                self._without_candidate(
                    request,
                    combined_observation,
                    status="baseline_stored",
                    weather_meta=weather_meta,
                )
            )

        flight_stale = parse_timestamp(flight["source_event_time"]) <= parse_timestamp(
            previous_flight["source_event_time"]
        )
        effective_flight = previous_flight if flight_stale else flight
        flight_fields = []
        if not flight_stale:
            flight_fields = changed_fields(
                {**previous_flight, "weather": {}},
                {**flight, "weather": {}},
            )
        weather_fields = _weather_fields(previous_weather, current_weather)
        fields = flight_fields + weather_fields

        combined_observation = {**effective_flight, "weather": current_weather}
        if not fields:
            if not flight_stale:
                self._store.put_last_observation(request.trip_id, request.leg_id, flight)
            if current_weather is not None:
                self._store.put_last_weather(
                    request.trip_id, request.leg_id, current_weather
                )
            return self._set_outcome(
                self._without_candidate(
                    request,
                    combined_observation,
                    status="stale_observation" if flight_stale else "unchanged",
                    weather_meta=weather_meta,
                )
            )

        before_delay = operational_delay(previous_flight)
        after_delay = operational_delay(effective_flight)
        classification_observation = {
            **effective_flight,
            "weather": current_weather or previous_weather or {},
        }
        category = classify_candidate(
            classification_observation,
            fields,
            None,
            None,
        )
        weather_risk = (
            str(current_weather["risk_level"])
            if current_weather is not None
            else "unknown"
        )
        corroborated = (
            category in {"DELAY", "CANCELLATION", "DIVERSION", "CONNECTION_RISK"}
            and weather_risk in {"high", "severe"}
        )
        confidence = float(effective_flight["confidence"])
        if category == "WEATHER_RISK" and current_weather is not None:
            confidence = float(current_weather["confidence"])
        elif corroborated and current_weather is not None:
            confidence = min(confidence, float(current_weather["confidence"]))

        evidence_id = str(effective_flight["observation_id"])
        if not flight_fields and current_weather is not None:
            evidence_id = str(current_weather["observation_id"])
        delta_id = _event_id("delta", request, evidence_id)
        candidate_id = _event_id("cand", request, evidence_id)
        delta = {
            "schema_version": "1.0.0",
            "delta_id": delta_id,
            "trip_id": request.trip_id,
            "leg_id": request.leg_id,
            "previous_observation_id": previous_flight["observation_id"],
            "current_observation_id": effective_flight["observation_id"],
            "observed_at": (
                current_weather["observed_at"]
                if not flight_fields and current_weather is not None
                else effective_flight["observed_at"]
            ),
            "changed_fields": fields,
            "delay_minutes_before": before_delay,
            "delay_minutes_after": after_delay,
            "delay_change_minutes": after_delay - before_delay,
        }
        evidence_ids = [
            previous_flight["observation_id"],
            effective_flight["observation_id"],
        ]
        if previous_weather is not None:
            evidence_ids.append(previous_weather["observation_id"])
        if current_weather is not None:
            evidence_ids.append(current_weather["observation_id"])
        candidate = {
            "schema_version": "1.0.0",
            "candidate_id": candidate_id,
            "delta": delta,
            "trip_id": request.trip_id,
            "leg_id": request.leg_id,
            "observed_at": delta["observed_at"],
            "category": category,
            "delay_minutes": after_delay,
            "connection_buffer_minutes": None,
            "minimum_connection_minutes": None,
            "weather_risk_level": weather_risk,
            "weather_evidence_status": weather_meta["status"],
            "corroborated_by_weather": corroborated,
            "confidence": confidence,
            "score": candidate_score(category, after_delay, confidence),
            "evidence_observation_ids": list(dict.fromkeys(evidence_ids)),
        }

        try:
            durable_writer = getattr(
                self._store, "put_candidate_with_outbox", None
            )
            if callable(durable_writer):
                durable_writer(candidate)
            else:
                self._store.put_candidate(candidate)
            self._publisher.publish_candidate(candidate)
            outbox_deleter = getattr(self._store, "delete_outbox", None)
            if callable(outbox_deleter):
                outbox_deleter(
                    "disruption_candidate", str(candidate["candidate_id"])
                )
        except Exception:
            return self._set_outcome(
                MonitoringPollOutcome(
                    status="poll_failed",
                    request=request,
                    observation=combined_observation,
                    candidate=candidate,
                    error_code="CANDIDATE_EVENT_PUBLISH_FAILED",
                    orchestration={
                        "framework": "crewai-flow",
                        "steps": [
                            "mcp.get_flight_status",
                            "mcp.get_airport_weather",
                            "dynamodb.diff_last_evidence",
                            "nats.publish_failed",
                        ],
                        "mcp_calls": 2,
                        "weather_evidence": weather_meta,
                        "notification_action": {"status": "not_required"},
                        "search_action": {"status": "not_required"},
                        "candidate_events_published": 0,
                    },
                )
            )

        self._persist(request, flight if not flight_stale else None, current_weather)
        decision, confirmed_event = self._store.wait_for_decision(
            candidate["candidate_id"], timeout_seconds=5
        )
        notification = None
        notification_status = "not_required"
        search = None
        search_status = "not_required"
        if decision and confirmed_event:
            notification = self._store.wait_for_notification(
                decision["decision_id"], timeout_seconds=5
            )
            notification_status = (
                str(notification["status"])
                if notification is not None
                else "pending"
            )
            if decision.get("verdict") == "NOTIFY_AND_SEARCH":
                search = self._store.wait_for_search(
                    decision["decision_id"], timeout_seconds=5
                )
                search_status = (
                    str(search["status"]) if search is not None else "pending"
                )
        return self._set_outcome(
            MonitoringPollOutcome(
                status="candidate_evaluated" if decision else "evaluation_pending",
                request=request,
                observation=combined_observation,
                candidate=candidate,
                decision=decision,
                confirmed_event=confirmed_event,
                notification=notification,
                search=search,
                orchestration={
                    "framework": "crewai-flow",
                    "steps": [
                        "mcp.get_flight_status",
                        "mcp.get_airport_weather",
                        "dynamodb.diff_last_evidence",
                        "nats.publish_disruption_candidate",
                        "eval_agent.consume_candidate",
                        "a2a.communication.explain_disruption",
                        "notification_action.consume_confirmed",
                        "flight_search_action.consume_confirmed",
                    ],
                    "mcp_calls": 2,
                    "weather_evidence": weather_meta,
                    "notification_action": {"status": notification_status},
                    "search_action": {"status": search_status},
                    "candidate_events_published": 1,
                },
            )
        )

    def _persist(
        self,
        request: MonitoringPollRequest,
        flight: dict[str, Any] | None,
        weather: dict[str, Any] | None,
    ) -> None:
        if flight is not None:
            self._store.put_last_observation(request.trip_id, request.leg_id, flight)
        if weather is not None:
            self._store.put_last_weather(request.trip_id, request.leg_id, weather)

    @staticmethod
    def _without_candidate(
        request: MonitoringPollRequest,
        observation: dict[str, Any],
        *,
        status: str,
        weather_meta: dict[str, Any],
    ) -> MonitoringPollOutcome:
        return MonitoringPollOutcome(
            status=status,
            request=request,
            observation=observation,
            orchestration={
                "framework": "crewai-flow",
                "steps": [
                    "mcp.get_flight_status",
                    "mcp.get_airport_weather",
                    "dynamodb.diff_last_evidence",
                ],
                "mcp_calls": 2,
                "weather_evidence": weather_meta,
                "notification_action": {"status": "not_required"},
                "search_action": {"status": "not_required"},
                "candidate_events_published": 0,
            },
        )

    def _set_outcome(self, outcome: MonitoringPollOutcome) -> dict[str, Any]:
        self.state.outcome = outcome.model_dump(mode="json", exclude_none=True)
        return self.state.outcome


def monitoring_agent_trace_input(
    request: MonitoringPollRequest, **_kwargs: Any
) -> dict[str, Any]:
    return {
        "task": "Compare current flight and weather evidence with saved state.",
        "flight": {
            "flight_iata": request.flight_iata,
            "flight_date": request.flight_date,
        },
        "correlation": {
            "trip_ref": hash_reference(request.trip_id),
            "leg_ref": hash_reference(request.leg_id),
        },
        "replay_requested": request.replay_key is not None,
    }


def monitoring_agent_trace_output(result: dict[str, Any]) -> dict[str, Any]:
    observation = result.get("observation") or {}
    departure = observation.get("departure") or {}
    weather = observation.get("weather") or {}
    candidate = result.get("candidate") or {}
    decision = result.get("decision") or {}
    orchestration = result.get("orchestration") or {}
    return {
        "status": result.get("status"),
        "observation": {
            "source": observation.get("source"),
            "observed_at": observation.get("observed_at"),
            "status": observation.get("status"),
            "departure_airport": observation.get("departure_airport"),
            "destination_airport": observation.get("destination_airport"),
            "scheduled_departure_at": departure.get("scheduled_at"),
            "estimated_departure_at": departure.get("estimated_at"),
            "gate": departure.get("gate"),
            "weather_risk_level": weather.get("risk_level"),
            "confidence": observation.get("confidence"),
        },
        "candidate": {
            "candidate_ref": (
                hash_reference(candidate.get("candidate_id"))
                if candidate.get("candidate_id")
                else None
            ),
            "category": candidate.get("category"),
            "delay_minutes": candidate.get("delay_minutes"),
            "weather_risk_level": candidate.get("weather_risk_level"),
            "score": candidate.get("score"),
            "confidence": candidate.get("confidence"),
        },
        "decision": {
            "verdict": decision.get("verdict"),
            "reason_codes": decision.get("reason_codes", []),
            "policy_version": decision.get("policy_version"),
        },
        "actions": {
            "notification": (
                orchestration.get("notification_action") or {}
            ).get("status"),
            "search": (orchestration.get("search_action") or {}).get("status"),
            "candidate_events_published": orchestration.get(
                "candidate_events_published"
            ),
        },
        "error_code": result.get("error_code"),
    }


@traced(
    "agent.monitor.detect_disruption",
    service_name="monitor-agent",
    attributes=lambda request, **kwargs: {
        "travel.trip_ref": hash_reference(request.trip_id),
        "travel.leg_ref": hash_reference(request.leg_id),
    },
    result_outcome=lambda result: str(result.get("status", "unknown")),
    content_input=monitoring_agent_trace_input,
    content_output=monitoring_agent_trace_output,
)
def run_monitoring_flow(
    request: MonitoringPollRequest,
    *,
    flight_status: FlightStatusGateway | None = None,
    weather: WeatherGateway | None = None,
    store: MonitoringStore | None = None,
    publisher: CandidatePublisher | None = None,
) -> dict[str, Any]:
    resolved_store = store or DynamoMonitoringStateStore.from_environment()
    tools_url = os.getenv("TRAVEL_TOOLS_MCP_URL", "http://127.0.0.1:8003/mcp")
    resolved_flight_status = flight_status or StreamableHttpFlightStatusMcpClient(
        tools_url
    )
    # Explicit flight gateways are slice-03 unit-test paths. Production uses MCP.
    resolved_weather = weather or (
        NeutralWeatherGateway()
        if flight_status is not None
        else StreamableHttpWeatherMcpClient(tools_url)
    )
    flow = FlightMonitoringFlow(
        flight_status=resolved_flight_status,
        weather=resolved_weather,
        store=resolved_store,
        publisher=publisher
        or NatsCandidatePublisher(os.getenv("NATS_URL", "nats://127.0.0.1:4222")),
    )
    result = flow.kickoff(inputs={"request": request.model_dump(mode="json")})
    if not isinstance(result, dict):
        raise TypeError("FlightMonitoringFlow must return a JSON object")
    return result
