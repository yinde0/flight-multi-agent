from __future__ import annotations

import json
import os
import re

from typing import Literal, Protocol
from urllib.parse import quote

import httpx

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flight_agent.telemetry import traced


PROMPT_VERSION = "disruption-explanation-v1"
DEFAULT_AZURE_OPENAI_API_VERSION = "2024-10-21"
DEFAULT_MIN_CONFIDENCE = 0.80

Category = Literal[
    "CANCELLATION",
    "DIVERSION",
    "CONNECTION_RISK",
    "DELAY",
    "GATE_CHANGE",
    "TERMINAL_CHANGE",
    "WEATHER_RISK",
    "STATUS_CHANGE",
]
EvidenceField = Literal[
    "category",
    "delay_minutes",
    "connection_buffer_minutes",
    "minimum_connection_minutes",
    "weather_risk_level",
    "corroborated_by_weather",
]


class DisruptionExplanationError(RuntimeError):
    """Friendly wording could not be safely generated or validated."""


class DisruptionExplanationNotConfiguredError(DisruptionExplanationError):
    """Azure explanation mode was enabled without a complete deployment config."""


class DisruptionExplanationRequest(BaseModel):
    """PII-free facts approved by Eval; this contract carries no action authority."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    category: Category
    verdict: Literal["NOTIFY", "NOTIFY_AND_SEARCH"]
    reason_codes: list[str] = Field(min_length=1, max_length=5)
    delay_minutes: int | None = Field(default=None, ge=0, le=2_880)
    connection_buffer_minutes: int | None = Field(default=None, ge=-1_440, le=2_880)
    minimum_connection_minutes: int | None = Field(default=None, ge=0, le=1_440)
    weather_risk_level: Literal["none", "low", "moderate", "severe"] | None = None
    corroborated_by_weather: bool | None = None
    search_requested: bool

    @model_validator(mode="after")
    def validate_search_authority(self) -> "DisruptionExplanationRequest":
        expected = self.verdict == "NOTIFY_AND_SEARCH"
        if self.search_requested != expected:
            raise ValueError("Search wording must match the authoritative Eval verdict")
        return self


class ModelDisruptionExplanation(BaseModel):
    """Strict Azure output before local factual and safety validation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    prompt_version: Literal["disruption-explanation-v1"] = PROMPT_VERSION
    message: str = Field(min_length=15, max_length=300)
    facts_used: list[EvidenceField] = Field(min_length=1, max_length=6)
    confidence: float = Field(ge=0, le=1)


class DisruptionExplanation(BaseModel):
    """Validated wording returned by the Communication Agent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    prompt_version: Literal["disruption-explanation-v1"] = PROMPT_VERSION
    message: str = Field(min_length=15, max_length=300)
    status: Literal["generated", "fallback"]
    source: Literal["azure_openai", "deterministic"]
    model: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    error_code: Literal[
        "EXPLANATION_LLM_NOT_CONFIGURED",
        "EXPLANATION_LLM_FAILED",
        "EXPLANATION_LLM_UNSAFE",
        "EXPLANATION_AGENT_UNAVAILABLE",
    ] | None = None


class DisruptionExplanationProvider(Protocol):
    provider_name: str
    model_name: str

    def explain(
        self, request: DisruptionExplanationRequest
    ) -> ModelDisruptionExplanation: ...


class AzureOpenAIDisruptionExplanationProvider:
    """Azure OpenAI adapter restricted to one PII-free structured wording task."""

    provider_name = "azure_openai"

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        deployment: str,
        api_version: str = DEFAULT_AZURE_OPENAI_API_VERSION,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key.strip()
        self._deployment = deployment.strip()
        self._api_version = api_version.strip()
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self.model_name = self._deployment or "unconfigured"

    @property
    def configured(self) -> bool:
        return bool(self._endpoint and self._api_key and self._deployment)

    @classmethod
    def from_environment(cls) -> "AzureOpenAIDisruptionExplanationProvider":
        return cls(
            endpoint=os.getenv(
                "AZURE_OPENAI_ENDPOINT", os.getenv("AZURE_ENDPOINT", "")
            ),
            api_key=os.getenv(
                "AZURE_OPENAI_API_KEY", os.getenv("AZURE_API_KEY", "")
            ),
            deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
            or os.getenv("CHAT_DEPLOYMENT", ""),
            api_version=os.getenv(
                "AZURE_OPENAI_API_VERSION",
                os.getenv("CHAT_API_VERSION", DEFAULT_AZURE_OPENAI_API_VERSION),
            ),
            timeout_seconds=float(
                os.getenv("DISRUPTION_EXPLANATION_TIMEOUT_SECONDS", "30")
            ),
        )

    def explain(
        self, request: DisruptionExplanationRequest
    ) -> ModelDisruptionExplanation:
        if not self.configured:
            raise DisruptionExplanationNotConfiguredError(
                "Azure OpenAI endpoint, API key, and deployment are required"
            )
        schema = ModelDisruptionExplanation.model_json_schema()
        properties = schema.get("properties", {})
        schema["required"] = list(properties)
        for definition in properties.values():
            if isinstance(definition, dict):
                definition.pop("default", None)
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"Prompt version: {PROMPT_VERSION}. Explain one confirmed "
                        "flight disruption in calm, friendly British English. Use only "
                        "the supplied operational facts. Do not mention internal reason "
                        "codes or identifiers. Do not invent gates, times, causes, "
                        "compensation, refunds, guarantees, or completed travel actions. "
                        "Explain only what changed; the application adds any next step. "
                        "Return one short sentence, or two very short sentences."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        request.model_dump(mode="json", exclude_none=True),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 220,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "disruption_explanation",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        deployment = quote(self._deployment, safe="")
        url = (
            f"{self._endpoint}/openai/deployments/{deployment}/chat/completions"
        )
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
                trust_env=self._transport is None,
            ) as client:
                response = client.post(
                    url,
                    params={"api-version": self._api_version},
                    headers={
                        "api-key": self._api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise DisruptionExplanationError(
                "Azure OpenAI explanation request failed"
            ) from error

        try:
            message = body["choices"][0]["message"]
            if message.get("refusal"):
                raise DisruptionExplanationError(
                    "Azure OpenAI refused the explanation"
                )
            content = message["content"]
            if not isinstance(content, str):
                raise TypeError("Structured output content must be text")
            return ModelDisruptionExplanation.model_validate(json.loads(content))
        except DisruptionExplanationError:
            raise
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise DisruptionExplanationError(
                "Azure OpenAI returned invalid structured output"
            ) from error


def explanation_mode() -> str:
    mode = os.getenv("DISRUPTION_EXPLANATION_MODE", "auto").strip().lower()
    if mode not in {"auto", "off", "azure"}:
        raise ValueError(
            "DISRUPTION_EXPLANATION_MODE must be auto, off, or azure"
        )
    return mode


def explanation_provider_from_environment(
) -> DisruptionExplanationProvider | None:
    mode = explanation_mode()
    if mode == "off":
        return None
    provider = AzureOpenAIDisruptionExplanationProvider.from_environment()
    if mode == "auto" and not provider.configured:
        return None
    return provider


def deterministic_explanation(
    request: DisruptionExplanationRequest,
    *,
    error_code: str | None = None,
) -> DisruptionExplanation:
    delay = request.delay_minutes
    messages = {
        "CANCELLATION": "Your flight has been cancelled.",
        "DIVERSION": "Your flight has been diverted to a different airport.",
        "CONNECTION_RISK": "This disruption may put your connection at risk.",
        "DELAY": (
            f"Your flight is now delayed by {delay} minutes."
            if delay is not None
            else "Your flight has a significant delay."
        ),
        "GATE_CHANGE": "Your flight's departure gate has changed.",
        "TERMINAL_CHANGE": "Your flight's terminal has changed.",
        "WEATHER_RISK": "Severe weather may affect your flight.",
        "STATUS_CHANGE": "There has been a significant change to your flight.",
    }
    return DisruptionExplanation(
        message=messages[request.category],
        status="fallback",
        source="deterministic",
        error_code=error_code,
    )


_CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "CANCELLATION": ("cancel",),
    "DIVERSION": ("divert",),
    "CONNECTION_RISK": ("connect",),
    "DELAY": ("delay",),
    "GATE_CHANGE": ("gate",),
    "TERMINAL_CHANGE": ("terminal",),
    "WEATHER_RISK": ("weather",),
    "STATUS_CHANGE": ("status", "change"),
}
_UNSUPPORTED_CLAIMS = re.compile(
    r"\b(?:booked|rebooked|reserved|refund(?:ed)?|compensation|guaranteed?|"
    r"alternative flights?|new flight)\b",
    re.IGNORECASE,
)


def _validated_model_message(
    request: DisruptionExplanationRequest,
    generated: ModelDisruptionExplanation,
    *,
    min_confidence: float,
) -> str:
    message = " ".join(generated.message.split())
    if generated.confidence < min_confidence:
        raise DisruptionExplanationError("Explanation confidence is below threshold")
    if re.search(r"https?://|www\.", message, re.IGNORECASE):
        raise DisruptionExplanationError("Explanation contains an unsupported link")
    if _UNSUPPORTED_CLAIMS.search(message):
        raise DisruptionExplanationError("Explanation claims an unsupported action")
    if re.search(r"\b[A-Z]{3,}[A-Z0-9]*_[A-Z0-9_]+\b", message):
        raise DisruptionExplanationError("Explanation exposes an internal reason code")
    lowered = message.casefold()
    if not any(
        term in lowered for term in _CATEGORY_TERMS[request.category]
    ):
        raise DisruptionExplanationError("Explanation does not describe its category")

    available = {
        "category",
        *(
            key
            for key in (
                "delay_minutes",
                "connection_buffer_minutes",
                "minimum_connection_minutes",
                "weather_risk_level",
                "corroborated_by_weather",
            )
            if getattr(request, key) is not None
        ),
    }
    if any(fact not in available for fact in generated.facts_used):
        raise DisruptionExplanationError("Explanation cites an unavailable fact")

    allowed_numbers = {
        str(value)
        for value in (
            request.delay_minutes,
            request.connection_buffer_minutes,
            request.minimum_connection_minutes,
        )
        if value is not None
    }
    if any(number not in allowed_numbers for number in re.findall(r"\d+", message)):
        raise DisruptionExplanationError("Explanation invents a numeric fact")
    return message


def explanation_trace_input(
    request: DisruptionExplanationRequest,
    provider: DisruptionExplanationProvider | None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, object]:
    return {
        "task": "Explain an Eval-approved disruption in friendly language.",
        "prompt_version": PROMPT_VERSION,
        "authoritative_source": "deterministic_eval_policy",
        "facts": request.model_dump(mode="json", exclude_none=True),
        "provider": provider.provider_name if provider else "deterministic",
        "model": provider.model_name if provider else None,
        "minimum_confidence": min_confidence,
        "contains_traveler_pii": False,
    }


@traced(
    "agent.communication.explain_disruption",
    service_name="communication-agent",
    kind="chain",
    attributes=lambda request, provider, min_confidence=DEFAULT_MIN_CONFIDENCE: {
        "gen_ai.request.model": (
            provider.model_name if provider is not None else "deterministic-template"
        ),
        "travel.communication.prompt_version": PROMPT_VERSION,
        "travel.disruption.category": request.category,
        "travel.disruption.verdict": request.verdict,
    },
    result_outcome=lambda result: result.status,
    content_input=explanation_trace_input,
    content_output=lambda result: result.model_dump(mode="json", exclude_none=True),
)
def explain_disruption(
    request: DisruptionExplanationRequest,
    provider: DisruptionExplanationProvider | None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> DisruptionExplanation:
    """Generate wording without ever blocking or changing the approved action."""

    if provider is None:
        return deterministic_explanation(request)
    try:
        generated = provider.explain(request)
        message = _validated_model_message(
            request, generated, min_confidence=min_confidence
        )
        return DisruptionExplanation(
            message=message,
            status="generated",
            source="azure_openai",
            model=provider.model_name,
            confidence=generated.confidence,
        )
    except DisruptionExplanationNotConfiguredError:
        return deterministic_explanation(
            request, error_code="EXPLANATION_LLM_NOT_CONFIGURED"
        )
    except DisruptionExplanationError as error:
        unsafe = any(
            token in str(error).casefold()
            for token in (
                "unsupported",
                "reason code",
                "category",
                "unavailable fact",
                "numeric fact",
                "confidence",
            )
        )
        return deterministic_explanation(
            request,
            error_code=(
                "EXPLANATION_LLM_UNSAFE" if unsafe else "EXPLANATION_LLM_FAILED"
            ),
        )
    except Exception:
        return deterministic_explanation(
            request, error_code="EXPLANATION_LLM_FAILED"
        )
