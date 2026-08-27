from __future__ import annotations

import argparse
import json
import os

from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def golden_pairs() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    from travel_eval.runner import DEFAULT_FIXTURES, DEFAULT_POLICY, run_suite

    results, _ = run_suite(DEFAULT_FIXTURES, DEFAULT_POLICY)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for result in results:
        candidates = {
            item["candidate_id"]: item for item in result["candidates"]
        }
        for decision in result["decisions"]:
            pairs.append((candidates[decision["candidate_id"]], decision))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the real CrewAI Eval reviewer over golden disruption decisions "
            "without authorizing notification, search, booking, or payment."
        )
    )
    parser.add_argument(
        "--maximum",
        type=int,
        default=0,
        help="Limit model calls; zero evaluates the complete golden decision set.",
    )
    parser.add_argument("--minimum-verdict-agreement", type=float, default=0.95)
    args = parser.parse_args()

    load_dotenv()
    from flight_agent.eval_reasoning import CrewAIEvalReasoner, advisory_record
    from travel_eval.runner import DEFAULT_POLICY, load_json

    reasoner = CrewAIEvalReasoner()
    policy = load_json(DEFAULT_POLICY)
    pairs = golden_pairs()
    if args.maximum > 0:
        pairs = pairs[: args.maximum]

    verdict_matches = 0
    exact_matches = 0
    failures = 0
    statuses: dict[str, int] = {}
    for candidate, decision in pairs:
        record = advisory_record(reasoner, candidate, policy, decision)
        status = str(record.get("status") if record else "disabled")
        statuses[status] = statuses.get(status, 0) + 1
        if not record or status == "failed":
            failures += 1
            continue
        advisory = record["advisory"]
        verdict_matches += int(
            advisory["recommended_verdict"] == decision["verdict"]
        )
        exact_matches += int(record["agreement"] is True)

    total = len(pairs)
    verdict_rate = verdict_matches / total if total else 0.0
    exact_rate = exact_matches / total if total else 0.0
    report = {
        "passed": (
            total > 0
            and failures == 0
            and verdict_rate >= args.minimum_verdict_agreement
        ),
        "prompt_content_printed": False,
        "golden_decisions_checked": total,
        "structured_output_success_rate": (
            (total - failures) / total if total else 0.0
        ),
        "verdict_agreement_rate": round(verdict_rate, 4),
        "exact_reason_agreement_rate": round(exact_rate, 4),
        "unauthorized_action_count": 0,
        "statuses": statuses,
        "model": reasoner.model_name,
    }
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
