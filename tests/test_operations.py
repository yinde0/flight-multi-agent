from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from flight_agent import operations_service
from flight_agent.event_delivery import NOTIFICATION_CONSUMER
from flight_agent.operations_service import _metric_lines, _record_for_redrive
from flight_agent.telemetry import hash_reference, metrics_text, traced


def confirmed_event() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "event_type": "disruption_confirmed",
        "candidate_id": "cand-v9-unit",
        "decision_id": "decision-v9-unit",
        "trip_id": "trip-v9-unit",
        "leg_id": "leg-v9-unit",
        "category": "CANCELLATION",
        "verdict": "NOTIFY_AND_SEARCH",
        "reason_codes": ["FLIGHT_CANCELLED"],
        "published_at": "2026-09-15T06:10:00Z",
    }


class FakeJetStream:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def stream_info(self, name):
        return SimpleNamespace(
            state=SimpleNamespace(messages=1, consumer_count=3)
        )

    async def consumer_info(self, stream, consumer):
        return SimpleNamespace(num_pending=0, num_ack_pending=0)

    async def publish(self, subject, payload, **kwargs):
        self.published.append(
            {"subject": subject, "payload": payload, **kwargs}
        )
        return SimpleNamespace(stream="TRAVEL_DISRUPTIONS_V1", seq=2)


class FakeConnection:
    def __init__(self) -> None:
        self.js = FakeJetStream()

    def jetstream(self):
        return self.js

    async def drain(self):
        return None


class FakeOperationsStore:
    def __init__(self) -> None:
        self.dead_letter = {
            "consumer": NOTIFICATION_CONSUMER,
            "event_id": "decision-v9-unit",
            "payload": confirmed_event(),
            "error_code": "NOTIFICATION_MCP_FAILED",
            "attempts": 3,
            "status": "active",
            "recorded_at": "2026-09-15T06:11:00Z",
            "redrive_request_id": None,
            "redriven_at": None,
        }
        self.claims: dict[str, dict[str, Any]] = {}
        self.finished: list[dict[str, Any]] = []

    def outbox_count(self, event_type):
        return 0

    def dead_letter_count(self, consumer):
        return int(
            consumer == NOTIFICATION_CONSUMER
            and self.dead_letter["status"] == "active"
        )

    def list_dead_letters(self, consumer, *, active_only=True):
        if self.dead_letter_count(consumer):
            return [dict(self.dead_letter)]
        return []

    def get_dead_letter(self, consumer, event_id):
        if (
            consumer == NOTIFICATION_CONSUMER
            and event_id == self.dead_letter["event_id"]
        ):
            return dict(self.dead_letter)
        return None

    def claim_redrive(self, **kwargs):
        request_id = kwargs["request_id"]
        if request_id in self.claims:
            return False, self.claims[request_id]
        claim = {**kwargs, "status": "publishing"}
        self.claims[request_id] = claim
        return True, claim

    def finish_redrive(self, **kwargs):
        self.finished.append(kwargs)
        self.claims[kwargs["request_id"]]["status"] = kwargs["status"]

    def mark_dead_letter_redriven(self, **kwargs):
        self.dead_letter["status"] = "redrive_published"
        self.dead_letter["redrive_request_id"] = kwargs["request_id"]


def test_redrive_record_rejects_mismatched_authority() -> None:
    payload = confirmed_event()
    record = _record_for_redrive(
        NOTIFICATION_CONSUMER, "decision-v9-unit", payload
    )

    assert record["event_type"] == "disruption_confirmed"
    assert record["event_id"] == "decision-v9-unit"

    payload["decision_id"] = "decision-forged"
    try:
        _record_for_redrive(
            NOTIFICATION_CONSUMER, "decision-v9-unit", payload
        )
    except ValueError as error:
        assert "event ID" in str(error)
    else:
        raise AssertionError("mismatched authority must fail")


def test_operations_routes_require_auth_and_redrive_once(monkeypatch) -> None:
    connection = FakeConnection()

    async def fake_connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(operations_service, "connect_nats", fake_connect)
    monkeypatch.setenv("OPS_API_TOKEN", "unit-ops-token")
    store = FakeOperationsStore()
    app = operations_service.create_operations_app(store)
    command = {
        "request_id": "redrive-v9-unit",
        "operator_ref": "operator:unit",
        "reason": "The notification dependency has been repaired.",
    }
    url = (
        "/v1/operations/dead-letters/"
        f"{NOTIFICATION_CONSUMER}/decision-v9-unit/redrive"
    )

    with TestClient(app) as client:
        assert client.get("/v1/operations/status").status_code == 401
        first = client.post(
            url,
            headers={"x-ops-token": "unit-ops-token"},
            json=command,
        )
        duplicate = client.post(
            url,
            headers={"x-ops-token": "unit-ops-token"},
            json=command,
        )

    assert first.status_code == 200
    assert first.json()["status"] == "published"
    assert duplicate.json()["status"] == "already_redriven"
    assert len(connection.js.published) == 1
    assert store.dead_letter["status"] == "redrive_published"
    assert store.finished[0]["status"] == "published"


def test_operations_routes_are_disabled_without_explicit_token(
    monkeypatch,
) -> None:
    connection = FakeConnection()

    async def fake_connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(operations_service, "connect_nats", fake_connect)
    monkeypatch.delenv("OPS_API_TOKEN", raising=False)
    app = operations_service.create_operations_app(FakeOperationsStore())

    with TestClient(app) as client:
        response = client.get("/v1/operations/status")

    assert response.status_code == 503
    assert response.json()["detail"] == "Operations access is disabled"


def test_operational_metrics_have_stable_low_cardinality_labels() -> None:
    rendered = _metric_lines(
        outboxes={"disruption_candidate": 1},
        dead_letters={NOTIFICATION_CONSUMER: 2},
        consumers={
            NOTIFICATION_CONSUMER: {"pending": 3, "ack_pending": 1}
        },
    )

    assert (
        'travel_dead_letters_active{consumer="travel-notification-action-v1"} 2'
        in rendered
    )
    assert "travel_jetstream_consumer_pending" in rendered


def test_trace_metrics_do_not_expose_raw_travel_reference(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_TRACING_ENABLED", "false")
    raw_reference = "trip-private-confirmation-ABC123"

    @traced(
        "unit.private",
        service_name="unit-service",
        attributes=lambda value: {
            "travel.trip_ref": hash_reference(value)
        },
    )
    def observed(value: str) -> str:
        return value

    assert observed(raw_reference) == raw_reference
    rendered = metrics_text()
    assert raw_reference not in rendered
    assert 'operation="unit.private",outcome="success"' in rendered
