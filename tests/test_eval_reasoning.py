from __future__ import annotations

import json
import os
import tempfile

from pathlib import Path
from typing import Any

import pytest

os.environ["LOCALAPPDATA"] = str(Path(tempfile.gettempdir()) / "flight-eval-test")
os.environ.setdefault(
    "CREWAI_STORAGE_DIR", str(Path(tempfile.gettempdir()) / "flight-eval-test-crewai")
)
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_DISABLE_TRACKING", "true")
os.environ.setdefault("CREWAI_DISABLE_VERSION_CHECK", "true")

from crewai.llms.base_llm import BaseLLM

from flight_agent.eval_reasoning import (
    CrewAIEvalReasoner,
    EvalAdvisory,
    advisory_record,
    advisory_trace_input,
    parse_advisory_output,
)
from travel_eval.runner import DEFAULT_FIXTURES, DEFAULT_POLICY, load_json, run_suite


class GoldenReasoner:
    model_name = "golden-test-reviewer"

    def recommend(
        self,
        candidate: dict[str, Any],
        policy: dict[str, Any],
        deterministic_decision: dict[str, Any],
    ) -> EvalAdvisory:
        del candidate, policy
        return EvalAdvisory(
            recommended_verdict=deterministic_decision["verdict"],
            reason_codes=deterministic_decision["reason_codes"],
            confidence=1.0,
            rationale="The versioned deterministic rule and evidence agree.",
        )


class FailingReasoner:
    model_name = "failing-test-reviewer"

    def recommend(self, candidate, policy, deterministic_decision):
        del candidate, policy, deterministic_decision
        raise RuntimeError("synthetic model outage")


class StaticStructuredLLM(BaseLLM):
    def call(
        self,
        messages,
        tools=None,
        callbacks=None,
        available_functions=None,
        from_task=None,
        from_agent=None,
        response_model=None,
    ):
        del (
            messages,
            tools,
            callbacks,
            available_functions,
            from_task,
            from_agent,
        )
        value = {
            "schema_version": "1.0.0",
            "prompt_version": "eval-advisory-v1",
            "recommended_verdict": "NOTIFY",
            "reason_codes": ["DELAY_NOTIFY_THRESHOLD"],
            "confidence": 0.99,
            "rationale": "The 45 minute delay exceeds the notification threshold.",
        }
        return response_model.model_validate(value) if response_model else json.dumps(value)


def _candidate() -> dict[str, Any]:
    return {
        "candidate_id": "cand-eval-unit",
        "trip_id": "trip-private-reference",
        "leg_id": "leg-private-reference",
        "category": "DELAY",
        "delay_minutes": 45,
        "weather_risk_level": "moderate",
        "confidence": 0.98,
        "score": 0.56,
        "provider_payload": {"must": "not appear"},
    }


def _decision() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "decision_id": "decision-eval-unit",
        "candidate_id": "cand-eval-unit",
        "trip_id": "trip-private-reference",
        "leg_id": "leg-private-reference",
        "decided_at": "2026-09-15T06:05:00Z",
        "verdict": "NOTIFY",
        "reason_codes": ["DELAY_NOTIFY_THRESHOLD"],
        "policy_version": "1.2.0",
        "confidence": 0.98,
        "cooldown_until": "2026-09-15T07:05:00Z",
    }


def test_advisory_trace_input_whitelists_non_traveler_evidence() -> None:
    value = advisory_trace_input(_candidate(), {"policy_version": "1.2.0"}, _decision())

    assert value["candidate"]["category"] == "DELAY"
    assert "trip_id" not in value["candidate"]
    assert "leg_id" not in value["candidate"]
    assert "provider_payload" not in value["candidate"]


def test_shadow_advisory_records_agreement_but_policy_stays_authoritative() -> None:
    decision = _decision()
    record = advisory_record(
        GoldenReasoner(),
        _candidate(),
        {"policy_version": "1.2.0"},
        decision,
    )

    assert record is not None
    assert record["status"] == "agreed"
    assert record["agreement"] is True
    assert record["policy_verdict"] == "NOTIFY"
    assert record["authoritative_source"] == "deterministic_policy"
    assert decision["verdict"] == "NOTIFY"


def test_reasoning_failure_is_audited_without_changing_policy_decision() -> None:
    decision = _decision()
    record = advisory_record(
        FailingReasoner(),
        _candidate(),
        {"policy_version": "1.2.0"},
        decision,
    )

    assert record is not None
    assert record["status"] == "failed"
    assert record["error_code"] == "EVAL_REASONING_FAILED"
    assert record["policy_verdict"] == decision["verdict"] == "NOTIFY"
    assert "advisory" not in record


def test_advisory_schema_rejects_unstructured_or_extra_authority() -> None:
    with pytest.raises(ValueError):
        EvalAdvisory(
            recommended_verdict="BOOK",  # type: ignore[arg-type]
            reason_codes=[],
            confidence=2,
            rationale="invalid",
            send_notification=True,  # type: ignore[call-arg]
        )


def test_advisory_parser_accepts_one_fenced_json_object_only() -> None:
    raw = """```json
{"schema_version":"1.0.0","prompt_version":"eval-advisory-v1","recommended_verdict":"SUPPRESS","reason_codes":["GATE_ONLY_CHANGE"],"confidence":1.0,"rationale":"A gate-only change is suppressed."}
```"""
    assert parse_advisory_output(raw).recommended_verdict == "SUPPRESS"
    with pytest.raises(ValueError):
        parse_advisory_output("Here is the answer: " + raw)


def test_real_crewai_task_returns_the_structured_advisory_contract() -> None:
    reasoner = CrewAIEvalReasoner(
        model_name="static-structured-test",
        llm=StaticStructuredLLM(model="static-structured-test"),
    )

    advisory = reasoner.recommend(
        _candidate(),
        load_json(DEFAULT_POLICY),
        _decision(),
    )

    assert advisory.recommended_verdict == "NOTIFY"
    assert advisory.reason_codes == ["DELAY_NOTIFY_THRESHOLD"]


def test_shadow_harness_covers_every_golden_decision() -> None:
    results, _ = run_suite(DEFAULT_FIXTURES, DEFAULT_POLICY)
    policy = load_json(DEFAULT_POLICY)
    checked = 0
    for result in results:
        candidates = {
            item["candidate_id"]: item for item in result["candidates"]
        }
        for decision in result["decisions"]:
            record = advisory_record(
                GoldenReasoner(),
                candidates[decision["candidate_id"]],
                policy,
                decision,
            )
            assert record is not None
            assert record["agreement"] is True
            checked += 1

    assert checked > 0
