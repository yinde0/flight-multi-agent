from __future__ import annotations

import json
import os
import tempfile

from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from flight_agent.telemetry import hash_reference, traced


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = (
    ROOT / "travel_eval" / "prompts" / "eval_advisory.v1.md"
)
PROMPT_VERSION = "eval-advisory-v1"
Verdict = Literal["SUPPRESS", "NOTIFY", "NOTIFY_AND_SEARCH"]


class EvalAdvisory(BaseModel):
    """Structured, non-authoritative output from the CrewAI reviewer."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    prompt_version: Literal["eval-advisory-v1"] = PROMPT_VERSION
    recommended_verdict: Verdict
    reason_codes: list[str] = Field(min_length=1, max_length=5)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=600)


class EvalReasoner(Protocol):
    model_name: str

    def recommend(
        self,
        candidate: dict[str, Any],
        policy: dict[str, Any],
        deterministic_decision: dict[str, Any],
    ) -> EvalAdvisory: ...


def candidate_trace_evidence(candidate: dict[str, Any]) -> dict[str, Any]:
    """Whitelist operational evidence; exclude traveler and provider payloads."""

    allowed = (
        "category",
        "delay_minutes",
        "connection_buffer_minutes",
        "minimum_connection_minutes",
        "weather_risk_level",
        "weather_evidence_status",
        "corroborated_by_weather",
        "confidence",
        "score",
    )
    evidence = {key: candidate.get(key) for key in allowed if key in candidate}
    if candidate.get("candidate_id"):
        evidence["candidate_ref"] = hash_reference(candidate["candidate_id"])
    return evidence


def _policy_evidence(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "policy_version": policy.get("policy_version"),
        "thresholds": policy.get("thresholds", {}),
        "ordered_rules": policy.get("ordered_rules", []),
        "safety_invariants": policy.get("safety_invariants", []),
    }


def _decision_evidence(decision: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "verdict",
        "reason_codes",
        "policy_version",
        "confidence",
        "cooldown_until",
    )
    return {key: decision.get(key) for key in allowed if key in decision}


def advisory_trace_input(
    candidate: dict[str, Any],
    policy: dict[str, Any],
    deterministic_decision: dict[str, Any],
) -> dict[str, Any]:
    return {
        "prompt_version": PROMPT_VERSION,
        "candidate": candidate_trace_evidence(candidate),
        "policy": _policy_evidence(policy),
        "deterministic_decision": _decision_evidence(deterministic_decision),
    }


def parse_advisory_output(raw: str) -> EvalAdvisory:
    """Accept one plain/fenced JSON object, then enforce the strict schema."""

    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Eval advisory contains no JSON object")
    if text[:start].strip() or text[end + 1 :].strip():
        raise ValueError("Eval advisory contains text outside its JSON object")
    return EvalAdvisory.model_validate(json.loads(text[start : end + 1]))


class CrewAIEvalReasoner:
    """One-tool-free CrewAI reviewer; it cannot execute traveler actions."""

    def __init__(
        self,
        *,
        model_name: str | None = None,
        api_key: str | None = None,
        prompt_path: Path | None = None,
        llm: Any | None = None,
    ) -> None:
        self.model_name = model_name or os.getenv(
            "EVAL_REASONING_MODEL", "mistral/mistral-small-latest"
        )
        self._prompt_path = prompt_path or Path(
            os.getenv("EVAL_REASONING_PROMPT_PATH", str(DEFAULT_PROMPT_PATH))
        )
        self._prompt_template = self._prompt_path.read_text(encoding="utf-8")
        if PROMPT_VERSION not in self._prompt_template:
            raise ValueError("Eval advisory prompt does not declare its version")
        if llm is not None:
            self._llm = llm
            return

        resolved_key = api_key or os.getenv("MISTRAL_API_KEY", "").strip()
        if not resolved_key:
            raise RuntimeError("MISTRAL_API_KEY is required for Eval reasoning")

        if os.name == "nt":
            os.environ["LOCALAPPDATA"] = str(
                Path(tempfile.gettempdir()) / "flight-eval-runtime"
            )
        os.environ.setdefault(
            "CREWAI_STORAGE_DIR",
            str(Path(tempfile.gettempdir()) / "flight-eval-crewai"),
        )
        os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
        os.environ.setdefault("CREWAI_DISABLE_TRACKING", "true")
        os.environ.setdefault("CREWAI_DISABLE_VERSION_CHECK", "true")

        from crewai import LLM

        class MistralCrewAILLM(LLM):
            """Strip CrewAI's provider hint before LiteLLM calls Mistral."""

            def _format_messages_for_provider(self, messages):
                cleaned = [
                    {
                        key: value
                        for key, value in message.items()
                        if key != "cache_breakpoint"
                    }
                    for message in messages
                ]
                return super()._format_messages_for_provider(cleaned)

        self._llm = MistralCrewAILLM(
            model=self.model_name,
            api_key=resolved_key,
            temperature=0,
            max_tokens=int(os.getenv("EVAL_REASONING_MAX_TOKENS", "700")),
            timeout=float(os.getenv("EVAL_REASONING_TIMEOUT_SECONDS", "45")),
        )

    @traced(
        "agent.eval.review_with_crewai",
        service_name="eval-agent",
        kind="chain",
        attributes=lambda self, candidate, policy, deterministic_decision: {
            "gen_ai.request.model": self.model_name,
            "travel.eval.prompt_version": PROMPT_VERSION,
        },
        result_outcome=lambda result: result.recommended_verdict.lower(),
        content_input=lambda self, candidate, policy, deterministic_decision: (
            advisory_trace_input(candidate, policy, deterministic_decision)
        ),
        content_output=lambda result: result.model_dump(mode="json"),
    )
    def recommend(
        self,
        candidate: dict[str, Any],
        policy: dict[str, Any],
        deterministic_decision: dict[str, Any],
    ) -> EvalAdvisory:
        from crewai import Agent, Crew, Process, Task

        description = self._prompt_template.format(
            candidate_json=json.dumps(
                candidate_trace_evidence(candidate),
                sort_keys=True,
                separators=(",", ":"),
            ),
            policy_json=json.dumps(
                _policy_evidence(policy), sort_keys=True, separators=(",", ":")
            ),
            decision_json=json.dumps(
                _decision_evidence(deterministic_decision),
                sort_keys=True,
                separators=(",", ":"),
            ),
            advisory_schema_json=json.dumps(
                EvalAdvisory.model_json_schema(),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        reviewer = Agent(
            role="Travel disruption decision reviewer",
            goal="Review one policy decision without taking or authorizing actions",
            backstory=(
                "You are a cautious evaluator. Deterministic policy is authoritative, "
                "and your structured output is advisory evidence only."
            ),
            llm=self._llm,
            tools=[],
            allow_delegation=False,
            allow_code_execution=False,
            memory=False,
            cache=False,
            max_iter=2,
            max_retry_limit=1,
            verbose=False,
        )
        task = Task(
            name=PROMPT_VERSION,
            description=description,
            expected_output=(
                "A schema-valid EvalAdvisory object with one verdict, one or more "
                "policy reason codes, confidence, and a brief rationale."
            ),
            agent=reviewer,
        )
        output = Crew(
            name="travel-eval-advisory",
            agents=[reviewer],
            tasks=[task],
            process=Process.sequential,
            memory=False,
            cache=False,
            planning=False,
            verbose=False,
        ).kickoff()
        advisory = output.pydantic
        if advisory is None and output.tasks_output:
            advisory = output.tasks_output[-1].pydantic
        if advisory is not None:
            return EvalAdvisory.model_validate(advisory)
        try:
            return parse_advisory_output(output.raw)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "CrewAI Eval task returned no schema-valid advisory"
            ) from error


def reasoning_mode() -> str:
    mode = os.getenv("EVAL_REASONING_MODE", "off").strip().lower()
    if mode not in {"off", "shadow"}:
        raise ValueError("EVAL_REASONING_MODE must be off or shadow")
    return mode


def reasoner_from_environment() -> EvalReasoner | None:
    return CrewAIEvalReasoner() if reasoning_mode() == "shadow" else None


def advisory_record(
    reasoner: EvalReasoner | None,
    candidate: dict[str, Any],
    policy: dict[str, Any],
    deterministic_decision: dict[str, Any],
) -> dict[str, Any] | None:
    """Run in shadow mode; a failure never changes deterministic authority."""

    if reasoner is None:
        return None
    base = {
        "schema_version": "1.0.0",
        "candidate_id": candidate["candidate_id"],
        "decision_id": deterministic_decision["decision_id"],
        "prompt_version": PROMPT_VERSION,
        "model": reasoner.model_name,
        "authoritative_source": "deterministic_policy",
        "policy_verdict": deterministic_decision["verdict"],
    }
    try:
        advisory = reasoner.recommend(candidate, policy, deterministic_decision)
    except Exception:
        return {
            **base,
            "status": "failed",
            "agreement": None,
            "error_code": "EVAL_REASONING_FAILED",
        }
    agreement = (
        advisory.recommended_verdict == deterministic_decision["verdict"]
        and advisory.reason_codes == deterministic_decision["reason_codes"]
    )
    return {
        **base,
        "status": "agreed" if agreement else "disagreed",
        "agreement": agreement,
        "advisory": advisory.model_dump(mode="json"),
    }
