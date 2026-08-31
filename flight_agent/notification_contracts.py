from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flight_agent.trip_contracts import E164_PATTERN


TWILIO_MESSAGE_SID_PATTERN = r"^(?:SM|MM)[0-9a-fA-F]{32}$"
TWILIO_MESSAGE_STATUS = Literal[
    "accepted",
    "scheduled",
    "queued",
    "sending",
    "sent",
    "delivered",
    "undelivered",
    "failed",
    "canceled",
]


class ConfirmedDisruptionEvent(BaseModel):
    """Event emitted by Eval only after a non-suppressed decision."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    event_type: Literal["disruption_confirmed"]
    candidate_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    trip_id: str = Field(min_length=1)
    leg_id: str = Field(min_length=1)
    category: Literal[
        "CANCELLATION",
        "DIVERSION",
        "CONNECTION_RISK",
        "DELAY",
        "GATE_CHANGE",
        "TERMINAL_CHANGE",
        "WEATHER_RISK",
        "STATUS_CHANGE",
    ]
    verdict: Literal["NOTIFY", "NOTIFY_AND_SEARCH"]
    reason_codes: list[str] = Field(min_length=1)
    published_at: str


class EvalApproval(BaseModel):
    """Decision proof passed to the notification capability."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    verdict: Literal["NOTIFY", "NOTIFY_AND_SEARCH"]
    policy_version: str = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)
    decided_at: str


class NotificationCommand(BaseModel):
    """Only input accepted by the isolated send_notification MCP tool."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    notification_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    trip_id: str = Field(min_length=1)
    leg_id: str = Field(min_length=1)
    recipient_ref: str = Field(pattern=r"^traveler:[a-z0-9-]+$")
    channel: Literal["push", "sms"] = "push"
    recipient_address: str | None = Field(default=None, pattern=E164_PATTERN)
    template: Literal["travel_disruption_v1"] = "travel_disruption_v1"
    template_variables: dict[str, str]
    search_requested: bool
    approval: EvalApproval

    @model_validator(mode="after")
    def validate_recipient_address(self) -> "NotificationCommand":
        if self.channel == "sms" and self.recipient_address is None:
            raise ValueError("SMS notification requires a recipient address")
        if self.channel != "sms" and self.recipient_address is not None:
            raise ValueError("Recipient address is only valid for SMS notifications")
        return self


class NotificationReceipt(BaseModel):
    """Provider-neutral result returned by the notification MCP."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    notification_id: str
    decision_id: str
    idempotency_key: str
    provider: str
    provider_delivery_id: str
    status: Literal["accepted", "delivered", "duplicate"]
    delivered_at: str
    provider_status: str | None = None


class NotificationSubmissionFailure(BaseModel):
    """Safe MCP failure result; never includes provider text, URLs, or PII."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["failed"] = "failed"
    provider: Literal["twilio"] = "twilio"
    error_code: str = Field(pattern=r"^TWILIO_(?:[0-9]{3,6}|HTTP_[0-9]{3}|SUBMISSION_UNCERTAIN|INVALID_RESPONSE)$")
    retryable: bool
    http_status: int | None = Field(default=None, ge=100, le=599)
    remediation: Literal[
        "upgrade_or_use_trial_template", "verify_recipient", "check_credentials",
        "check_sender", "provider_rejected", "retry_later", "check_delivery_before_retry",
    ] = "provider_rejected"


class NotificationActionRecord(BaseModel):
    """Durable audit record written by the post-Eval action service."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    notification_id: str
    candidate_id: str
    decision_id: str
    trip_id: str
    leg_id: str
    verdict: Literal["NOTIFY", "NOTIFY_AND_SEARCH"]
    status: Literal["accepted", "delivered", "duplicate", "failed", "rejected"]
    idempotency_key: str
    provider: str | None = None
    provider_delivery_id: str | None = None
    provider_status: str | None = None
    recorded_at: str
    delivery_updated_at: str | None = None
    error_code: str | None = None
    submission_failure: NotificationSubmissionFailure | None = None
    friendly_message: str | None = Field(default=None, max_length=300)
    explanation_status: Literal["generated", "fallback"] | None = None
    explanation_source: Literal["azure_openai", "deterministic"] | None = None
    explanation_model: str | None = None
    explanation_prompt_version: str | None = None
    explanation_error_code: str | None = None


class TwilioSmsStatusCallback(BaseModel):
    """PII-free subset selected only after the full request is authenticated."""

    model_config = ConfigDict(extra="forbid")

    account_sid: str = Field(pattern=r"^AC[0-9a-fA-F]{32}$")
    message_sid: str = Field(pattern=TWILIO_MESSAGE_SID_PATTERN)
    message_status: TWILIO_MESSAGE_STATUS
    error_code: int | None = Field(default=None, ge=0)


class DeliveryReconciliationOutcome(BaseModel):
    """Result of applying one authenticated provider delivery update."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["twilio"] = "twilio"
    provider_delivery_id: str = Field(pattern=TWILIO_MESSAGE_SID_PATTERN)
    found: bool
    applied: bool
    duplicate: bool = False
    ignored_reason: Literal[
        "UNKNOWN_DELIVERY",
        "DUPLICATE",
        "STALE_STATUS",
        "TERMINAL_STATUS",
    ] | None = None
    decision_id: str | None = None
    notification_id: str | None = None
    previous_provider_status: str | None = None
    provider_status: str | None = None
    action_status: Literal["accepted", "delivered", "failed"] | None = None
