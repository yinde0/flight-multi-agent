from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .clock import VirtualClock, format_timestamp, parse_timestamp
from .policy import PolicyState, SuppressionPolicy


def minutes_between(start: str, end: str) -> int:
    return int((parse_timestamp(end) - parse_timestamp(start)).total_seconds() // 60)


def effective_time(section: dict[str, Any]) -> str:
    return section.get("actual_at") or section.get("estimated_at") or section["scheduled_at"]


def movement_delay(section: dict[str, Any]) -> int:
    return max(0, minutes_between(section["scheduled_at"], effective_time(section)))


def operational_delay(observation: dict[str, Any]) -> int:
    """Use the larger of departure and arrival delay for significance decisions."""
    return max(
        movement_delay(observation["departure"]),
        movement_delay(observation["arrival"]),
    )


def changed_fields(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    paths = [
        ("status",),
        ("departure", "estimated_at"),
        ("departure", "actual_at"),
        ("departure", "terminal"),
        ("departure", "gate"),
        ("arrival", "estimated_at"),
        ("arrival", "actual_at"),
        ("arrival", "terminal"),
        ("arrival", "gate"),
        ("weather", "risk_level"),
        ("weather", "alerts"),
    ]
    result: list[str] = []
    for path in paths:
        left: Any = previous
        right: Any = current
        for part in path:
            left = left.get(part) if isinstance(left, dict) else None
            right = right.get(part) if isinstance(right, dict) else None
        if left != right:
            result.append(".".join(path))
    return result


def deep_merge(target: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Apply a compact scenario update while preserving a full canonical observation."""
    merged = deepcopy(target)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def expand_observations(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    if "observations" in scenario:
        return scenario["observations"]

    current = deepcopy(scenario["base_observation"])
    envelopes: list[dict[str, Any]] = []
    for item in scenario["timeline"]:
        current = deep_merge(current, item.get("changes", {}))
        current["observation_id"] = item["observation_id"]
        current["source_event_time"] = item["source_event_time"]
        current["observed_at"] = item["received_at"]
        envelopes.append(
            {
                "received_at": item["received_at"],
                "observation": deepcopy(current),
            }
        )
    return envelopes


def classify_candidate(
    current: dict[str, Any],
    fields: list[str],
    connection_buffer_minutes: int | None,
    minimum_connection_minutes: int | None,
) -> str:
    if current["status"] == "cancelled":
        return "CANCELLATION"
    if current["status"] == "diverted":
        return "DIVERSION"
    if (
        connection_buffer_minutes is not None
        and minimum_connection_minutes is not None
        and connection_buffer_minutes < minimum_connection_minutes
    ):
        return "CONNECTION_RISK"

    delay = operational_delay(current)
    delay_fields = {
        "departure.estimated_at",
        "departure.actual_at",
        "arrival.estimated_at",
        "arrival.actual_at",
    }
    if delay > 0 and delay_fields.intersection(fields):
        return "DELAY"

    field_set = set(fields)
    if field_set and field_set <= {"departure.gate", "arrival.gate"}:
        return "GATE_CHANGE"
    if field_set and field_set <= {
        "departure.terminal",
        "arrival.terminal",
        "departure.gate",
        "arrival.gate",
    }:
        return "TERMINAL_CHANGE"
    if field_set and field_set <= {"weather.risk_level", "weather.alerts"}:
        return "WEATHER_RISK"
    return "STATUS_CHANGE"


def candidate_score(category: str, delay: int, confidence: float) -> float:
    base = {
        "CANCELLATION": 1.0,
        "DIVERSION": 1.0,
        "CONNECTION_RISK": 0.95,
        "DELAY": min(0.9, 0.2 + delay / 120),
        "WEATHER_RISK": 0.45,
        "TERMINAL_CHANGE": 0.25,
        "GATE_CHANGE": 0.1,
        "STATUS_CHANGE": 0.2,
    }[category]
    return round(base * confidence, 4)


@dataclass
class ReplayResult:
    scenario_id: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    ignored_observation_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "candidates": self.candidates,
            "decisions": self.decisions,
            "notifications": self.notifications,
            "ignored_observation_ids": self.ignored_observation_ids,
        }


class ReplayEngine:
    def __init__(self, policy: SuppressionPolicy):
        self.policy = policy

    def run(self, scenario: dict[str, Any]) -> ReplayResult:
        scenario_id = scenario["scenario_id"]
        result = ReplayResult(scenario_id=scenario_id)
        clock = VirtualClock()
        policy_state = PolicyState()
        last_state: dict[str, dict[str, Any]] = {}
        last_source_event: dict[str, datetime] = {}
        sequence = 0

        observations = sorted(expand_observations(scenario), key=lambda item: parse_timestamp(item["received_at"]))
        for envelope in observations:
            clock.advance_to(envelope["received_at"])
            observation = deepcopy(envelope["observation"])
            leg_id = observation["leg_id"]
            source_time = parse_timestamp(observation["source_event_time"])

            if leg_id in last_source_event and source_time <= last_source_event[leg_id]:
                result.ignored_observation_ids.append(observation["observation_id"])
                continue

            previous = last_state.get(leg_id)
            last_state[leg_id] = observation
            last_source_event[leg_id] = source_time
            if previous is None:
                continue

            fields = changed_fields(previous, observation)
            if not fields:
                continue

            sequence += 1
            candidate_id = f"cand-{scenario_id}-{sequence:03d}"
            delta_id = f"delta-{scenario_id}-{sequence:03d}"
            before_delay = operational_delay(previous)
            after_delay = operational_delay(observation)

            connection_buffer = None
            minimum_connection = None
            connection = scenario.get("connection_context", {}).get(leg_id)
            if connection:
                arrival_at = effective_time(observation["arrival"])
                connection_buffer = minutes_between(arrival_at, connection["next_departure_at"])
                minimum_connection = connection["minimum_connection_minutes"]

            category = classify_candidate(
                observation,
                fields,
                connection_buffer,
                minimum_connection,
            )
            delta = {
                "schema_version": "1.0.0",
                "delta_id": delta_id,
                "trip_id": observation["trip_id"],
                "leg_id": leg_id,
                "previous_observation_id": previous["observation_id"],
                "current_observation_id": observation["observation_id"],
                "observed_at": format_timestamp(clock.now()),
                "changed_fields": fields,
                "delay_minutes_before": before_delay,
                "delay_minutes_after": after_delay,
                "delay_change_minutes": after_delay - before_delay,
            }
            candidate = {
                "schema_version": "1.0.0",
                "candidate_id": candidate_id,
                "delta": delta,
                "trip_id": observation["trip_id"],
                "leg_id": leg_id,
                "observed_at": format_timestamp(clock.now()),
                "category": category,
                "delay_minutes": after_delay,
                "connection_buffer_minutes": connection_buffer,
                "minimum_connection_minutes": minimum_connection,
                "weather_risk_level": observation.get("weather", {}).get("risk_level", "none"),
                "confidence": observation["confidence"],
                "score": candidate_score(category, after_delay, observation["confidence"]),
                "evidence_observation_ids": [
                    previous["observation_id"],
                    observation["observation_id"],
                ],
            }
            result.candidates.append(candidate)

            decision = self.policy.evaluate(
                candidate,
                policy_state,
                decision_id=f"decision-{scenario_id}-{sequence:03d}",
            )
            result.decisions.append(decision)
            if decision["verdict"] != "SUPPRESS":
                result.notifications.append(
                    self._notification_for(scenario_id, sequence, candidate, decision)
                )

        return result

    @staticmethod
    def _notification_for(
        scenario_id: str,
        sequence: int,
        candidate: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "notification_id": f"notification-{scenario_id}-{sequence:03d}",
            "decision_id": decision["decision_id"],
            "trip_id": candidate["trip_id"],
            "leg_id": candidate["leg_id"],
            "created_at": decision["decided_at"],
            "channel": "push",
            "template": "travel_disruption_v1",
            "reason_codes": decision["reason_codes"],
            "search_requested": decision["verdict"] == "NOTIFY_AND_SEARCH",
            "idempotency_key": (
                f"{candidate['trip_id']}:{candidate['leg_id']}:"
                f"{candidate['category']}:{self_band(candidate)}"
            ),
        }


def self_band(candidate: dict[str, Any]) -> int:
    return SuppressionPolicy.severity_band(candidate)
