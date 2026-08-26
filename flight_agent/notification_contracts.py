from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    channel: Literal["push"] = "push"
    template: Literal["travel_disruption_v1"] = "travel_disruption_v1"
    template_variables: dict[str, str]
    search_requested: bool
    approval: EvalApproval


class NotificationReceipt(BaseModel):
    """Provider-neutral result returned by the notification MCP."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    notification_id: str
    decision_id: str
    idempotency_key: str
    provider: str
    provider_delivery_id: str
    status: Literal["delivered", "duplicate"]
    delivered_at: str


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
    status: Literal["delivered", "duplicate", "failed", "rejected"]
    idempotency_key: str
    provider: str | None = None
    provider_delivery_id: str | None = None
    recorded_at: str
    error_code: str | None = None
