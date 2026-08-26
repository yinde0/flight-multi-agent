from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .clock import format_timestamp, parse_timestamp


@dataclass
class PolicyState:
    """Notification history used to suppress repeated severity bands."""

    highest_notified_band: dict[str, int] = field(default_factory=dict)


class SuppressionPolicy:
    def __init__(self, policy: dict[str, Any]):
        self.policy = policy
        self.version = policy["policy_version"]
        self.delay_thresholds = policy["thresholds"]["delay_minutes"]
        self.cooldown_minutes = policy["thresholds"]["cooldown_minutes"]

    @staticmethod
    def severity_band(candidate: dict[str, Any]) -> int:
        category = candidate["category"]
        if category in {"CANCELLATION", "DIVERSION", "CONNECTION_RISK"}:
            return 3
        delay = candidate.get("delay_minutes", 0)
        if delay >= 90:
            return 3
        if delay >= 30:
            return 2
        if delay > 0:
            return 1
        return 0

    def evaluate(
        self,
        candidate: dict[str, Any],
        state: PolicyState,
        decision_id: str,
    ) -> dict[str, Any]:
        category = candidate["category"]
        delay = candidate.get("delay_minutes", 0)
        reason_codes: list[str]

        if category == "CANCELLATION":
            verdict = "NOTIFY_AND_SEARCH"
            reason_codes = ["FLIGHT_CANCELLED"]
        elif category == "DIVERSION":
            verdict = "NOTIFY_AND_SEARCH"
            reason_codes = ["FLIGHT_DIVERTED"]
        elif category == "CONNECTION_RISK":
            verdict = "NOTIFY_AND_SEARCH"
            reason_codes = ["CONNECTION_BELOW_MINIMUM"]
        elif category == "DELAY" and delay >= self.delay_thresholds["search"]:
            verdict = "NOTIFY_AND_SEARCH"
            reason_codes = ["DELAY_SEARCH_THRESHOLD"]
        elif category == "DELAY" and delay >= self.delay_thresholds["notify"]:
            verdict = "NOTIFY"
            reason_codes = ["DELAY_NOTIFY_THRESHOLD"]
        elif category == "DELAY":
            verdict = "SUPPRESS"
            reason_codes = ["DELAY_BELOW_NOTIFY_THRESHOLD"]
        elif category == "GATE_CHANGE":
            verdict = "SUPPRESS"
            reason_codes = ["GATE_ONLY_CHANGE"]
        elif category == "TERMINAL_CHANGE":
            verdict = "SUPPRESS"
            reason_codes = ["TERMINAL_ONLY_CHANGE"]
        elif category == "WEATHER_RISK":
            verdict = "SUPPRESS"
            reason_codes = ["WEATHER_UNCORROBORATED"]
        else:
            verdict = "SUPPRESS"
            reason_codes = ["NON_ACTIONABLE_STATUS_CHANGE"]

        episode_key = f"{candidate['trip_id']}:{candidate['leg_id']}:{category}"
        band = self.severity_band(candidate)
        previous_band = state.highest_notified_band.get(episode_key, -1)

        if verdict != "SUPPRESS" and band <= previous_band:
            verdict = "SUPPRESS"
            reason_codes = ["DUPLICATE_SEVERITY_BAND"]
        elif verdict != "SUPPRESS":
            state.highest_notified_band[episode_key] = band

        decided_at = candidate["observed_at"]
        cooldown_until = format_timestamp(
            parse_timestamp(decided_at) + timedelta(minutes=self.cooldown_minutes)
        )
        return {
            "schema_version": "1.0.0",
            "decision_id": decision_id,
            "candidate_id": candidate["candidate_id"],
            "trip_id": candidate["trip_id"],
            "leg_id": candidate["leg_id"],
            "decided_at": decided_at,
            "verdict": verdict,
            "reason_codes": reason_codes,
            "policy_version": self.version,
            "confidence": candidate["confidence"],
            "cooldown_until": cooldown_until,
        }
