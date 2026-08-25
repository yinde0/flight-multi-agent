from __future__ import annotations

from typing import Any


def safe_ratio(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else numerator / denominator


def is_expected_subset(actual: Any, expected: Any) -> bool:
    """Match curated golden fields while allowing richer runtime evidence."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and is_expected_subset(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(is_expected_subset(a, e) for a, e in zip(actual, expected, strict=True))
        )
    return actual == expected


def calculate_metrics(results: list[dict[str, Any]], expected: list[dict[str, Any]]) -> dict[str, Any]:
    expected_by_scenario = {item["scenario_id"]: item for item in expected}
    scenario_passes = 0
    expected_notify = 0
    actual_notify = 0
    true_notify = 0
    duplicate_keys = 0
    unauthorized = 0
    notification_keys: set[str] = set()

    for actual in results:
        gold = expected_by_scenario[actual["scenario_id"]]
        if is_expected_subset(actual, gold):
            scenario_passes += 1

        gold_decisions = {d["candidate_id"]: d["verdict"] for d in gold["decisions"]}
        actual_decisions = {d["candidate_id"]: d["verdict"] for d in actual["decisions"]}
        expected_notify += sum(v != "SUPPRESS" for v in gold_decisions.values())
        actual_notify += sum(v != "SUPPRESS" for v in actual_decisions.values())
        true_notify += sum(
            verdict != "SUPPRESS"
            and gold_decisions.get(candidate_id) in {"NOTIFY", "NOTIFY_AND_SEARCH"}
            for candidate_id, verdict in actual_decisions.items()
        )

        approved_ids = {
            decision["decision_id"]
            for decision in actual["decisions"]
            if decision["verdict"] != "SUPPRESS"
        }
        for notification in actual["notifications"]:
            if notification["decision_id"] not in approved_ids:
                unauthorized += 1
            key = notification["idempotency_key"]
            if key in notification_keys:
                duplicate_keys += 1
            notification_keys.add(key)

    return {
        "scenario_pass_rate": round(safe_ratio(scenario_passes, len(expected)), 4),
        "notification_precision": round(safe_ratio(true_notify, actual_notify), 4),
        "material_disruption_recall": round(safe_ratio(true_notify, expected_notify), 4),
        "duplicate_notification_rate": round(safe_ratio(duplicate_keys, actual_notify), 4),
        "unauthorized_notification_count": unauthorized,
        "scenarios_evaluated": len(expected),
    }
