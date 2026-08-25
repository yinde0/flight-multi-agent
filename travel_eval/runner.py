from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .engine import ReplayEngine
from .metrics import calculate_metrics
from .policy import SuppressionPolicy


PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_FIXTURES = PACKAGE_ROOT / "fixtures" / "scenarios"
DEFAULT_POLICY = PACKAGE_ROOT / "policies" / "suppression_policy.v1.json"
DEFAULT_THRESHOLDS = PACKAGE_ROOT / "acceptance_thresholds.json"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_expected(case_dir: Path) -> dict[str, Any]:
    return {
        "scenario_id": load_json(case_dir / "scenario.json")["scenario_id"],
        "candidates": load_json(case_dir / "expected_candidates.json"),
        "decisions": load_json(case_dir / "expected_decisions.json"),
        "notifications": load_json(case_dir / "expected_notifications.json"),
        "ignored_observation_ids": load_json(case_dir / "expected_ignored_observations.json"),
    }


def threshold_failures(metrics: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for name, rule in thresholds["automated_replay_metrics"].items():
        value = metrics[name]
        if "minimum" in rule and value < rule["minimum"]:
            failures.append(f"{name}={value} is below {rule['minimum']}")
        if "maximum" in rule and value > rule["maximum"]:
            failures.append(f"{name}={value} is above {rule['maximum']}")
    return failures


def run_suite(fixtures: Path, policy_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    policy = SuppressionPolicy(load_json(policy_path))
    engine = ReplayEngine(policy)
    results: list[dict[str, Any]] = []
    expected: list[dict[str, Any]] = []

    case_dirs = sorted(path for path in fixtures.iterdir() if path.is_dir())
    if not case_dirs:
        raise ValueError(f"No replay scenarios found in {fixtures}")

    for case_dir in case_dirs:
        scenario = load_json(case_dir / "scenario.json")
        results.append(engine.run(scenario).as_dict())
        expected.append(load_expected(case_dir))
    return results, expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay the travel disruption golden scenarios")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--thresholds", type=Path, default=DEFAULT_THRESHOLDS)
    parser.add_argument("--show-results", action="store_true")
    args = parser.parse_args(argv)

    results, expected = run_suite(args.fixtures, args.policy)
    metrics = calculate_metrics(results, expected)
    failures = threshold_failures(metrics, load_json(args.thresholds))

    payload: dict[str, Any] = {"metrics": metrics, "threshold_failures": failures}
    if args.show_results:
        payload["results"] = results
    print(json.dumps(payload, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
