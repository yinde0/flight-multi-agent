from __future__ import annotations

import copy
import json

from pathlib import Path
from typing import Any

import pytest
import httpx

from pydantic import ValidationError

from flight_agent.flight_search import (
    DuffelFlightSearchProvider,
    DuffelFlightSearchProviderError,
    ReplayFlightSearchProvider,
    rank_feasible_options,
    run_provider_search,
)
from flight_agent.flight_search_action_service import process_search_event
from flight_agent.flight_search_contracts import (
    FlightSearchCommand,
    FlightSearchToolResult,
    SearchEvalApproval,
)


ROOT = Path(__file__).resolve().parents[1]
SEARCH_FIXTURE = (
    ROOT / "travel_eval" / "fixtures" / "search" / "vertical_06_options.json"
)
DUFFEL_FIXTURE = (
    ROOT / "travel_eval" / "fixtures" / "search" / "duffel_offer_response.json"
)


class SearchMemoryStore:
    def __init__(self) -> None:
        self.decisions: dict[str, dict[str, Any]] = {}
        self.confirmed: dict[str, dict[str, Any]] = {}
        self.observations: dict[str, dict[str, Any]] = {}
        self.searches: dict[str, dict[str, Any]] = {}

    @staticmethod
    def leg_key(trip_id: str, leg_id: str) -> str:
        return f"{trip_id}:{leg_id}"

    def get_decision(self, candidate_id):
        return copy.deepcopy(self.decisions.get(candidate_id))

    def get_confirmed_event(self, candidate_id):
        return copy.deepcopy(self.confirmed.get(candidate_id))

    def get_last_observation(self, trip_id, leg_id):
        return copy.deepcopy(self.observations.get(self.leg_key(trip_id, leg_id)))

    def get_search(self, decision_id):
        return copy.deepcopy(self.searches.get(decision_id))

    def put_search(self, decision_id, search):
        self.searches[decision_id] = copy.deepcopy(search)


class ReplaySearchGateway:
    def __init__(self) -> None:
        self.provider = ReplayFlightSearchProvider(SEARCH_FIXTURE)
        self.calls: list[FlightSearchCommand] = []

    def search_flights(self, command: FlightSearchCommand):
        self.calls.append(command)
        return run_provider_search(command, self.provider)


class FailingSearchGateway:
    def search_flights(self, command: FlightSearchCommand):
        del command
        raise RuntimeError("simulated search MCP outage")


def confirmed_event(verdict: str = "NOTIFY_AND_SEARCH") -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "event_type": "disruption_confirmed",
        "candidate_id": "cand-v6-001",
        "decision_id": "decision-v6-001",
        "trip_id": "trip-v6",
        "leg_id": "leg-v6",
        "category": "CANCELLATION",
        "verdict": verdict,
        "reason_codes": ["FLIGHT_CANCELLED"],
        "published_at": "2026-09-15T06:05:00Z",
    }


def authorized_store(verdict: str = "NOTIFY_AND_SEARCH") -> SearchMemoryStore:
    store = SearchMemoryStore()
    event = confirmed_event(verdict)
    store.confirmed[event["candidate_id"]] = copy.deepcopy(event)
    store.decisions[event["candidate_id"]] = {
        "schema_version": "1.0.0",
        "candidate_id": event["candidate_id"],
        "decision_id": event["decision_id"],
        "trip_id": event["trip_id"],
        "leg_id": event["leg_id"],
        "verdict": event["verdict"],
        "reason_codes": event["reason_codes"],
        "policy_version": "1.1.0",
        "decided_at": event["published_at"],
    }
    store.observations[store.leg_key(event["trip_id"], event["leg_id"])] = {
        "flight_iata": "NB204",
        "flight_date": "2026-09-15",
        "departure_airport": "LHR",
        "destination_airport": "CDG",
        "departure": {"scheduled_at": "2026-09-15T08:20:00Z"},
    }
    return store


def duffel_command() -> FlightSearchCommand:
    return FlightSearchCommand(
        search_id="search-duffel-001",
        idempotency_key="search:decision-duffel-001",
        trip_id="trip-duffel",
        leg_id="leg-duffel",
        original_flight_iata="NB204",
        origin="LHR",
        destination="CDG",
        departure_date="2026-09-15",
        earliest_departure_at="2026-09-15T08:00:00Z",
        latest_departure_at="2026-09-15T20:00:00Z",
        passenger_count=2,
        cabin_class="economy",
        approval=SearchEvalApproval(
            candidate_id="cand-duffel-001",
            decision_id="decision-duffel-001",
            verdict="NOTIFY_AND_SEARCH",
            policy_version="1.1.0",
            reason_codes=["FLIGHT_CANCELLED"],
            decided_at="2026-09-15T06:05:00Z",
        ),
    )


def test_authorized_search_filters_ranks_and_runs_once() -> None:
    store = authorized_store()
    gateway = ReplaySearchGateway()

    first = process_search_event(
        confirmed_event(),
        store=store,
        search_gateway=gateway,
        authority_timeout_seconds=0,
    )
    second = process_search_event(
        confirmed_event(),
        store=store,
        search_gateway=gateway,
        authority_timeout_seconds=0,
    )

    assert first.status == "completed"
    assert second == first
    assert len(gateway.calls) == 1
    assert [item.option_id for item in first.alternatives] == [
        "option-direct-fast",
        "option-one-stop",
    ]
    assert first.rejection_summary == {
        "CONNECTION_TOO_SHORT": 1,
        "ORIGINAL_FLIGHT": 1,
        "OUTSIDE_SEARCH_WINDOW": 1,
        "ROUTE_MISMATCH": 1,
        "TOO_MANY_STOPS": 1,
    }
    assert first.availability_verified is False
    assert first.booking_guaranteed is False
    assert first.booking_authorized is False
    assert store.searches[first.decision_id]["status"] == "completed"


def test_notify_without_search_verdict_never_calls_search_mcp() -> None:
    store = authorized_store("NOTIFY")
    gateway = ReplaySearchGateway()

    result = process_search_event(
        confirmed_event("NOTIFY"),
        store=store,
        search_gateway=gateway,
        authority_timeout_seconds=0,
    )

    assert result.status == "rejected"
    assert result.error_code == "SEARCH_NOT_AUTHORIZED"
    assert gateway.calls == []
    assert store.searches == {}


def test_forged_search_event_is_rejected_before_mcp() -> None:
    store = authorized_store()
    gateway = ReplaySearchGateway()
    event = confirmed_event()
    event["reason_codes"] = ["DELAY_SEARCH_THRESHOLD"]

    result = process_search_event(
        event,
        store=store,
        search_gateway=gateway,
        authority_timeout_seconds=0,
    )

    assert result.status == "rejected"
    assert result.error_code == "EVAL_AUTHORITY_MISMATCH"
    assert gateway.calls == []
    assert store.searches == {}


def test_search_mcp_failure_is_audited_without_fake_options() -> None:
    store = authorized_store()
    result = process_search_event(
        confirmed_event(),
        store=store,
        search_gateway=FailingSearchGateway(),
        authority_timeout_seconds=0,
    )

    assert result.status == "failed"
    assert result.error_code == "FLIGHT_SEARCH_MCP_FAILED"
    assert result.alternatives == []
    assert result.provider is None
    assert store.searches[result.decision_id]["status"] == "failed"


def test_notify_verdict_cannot_cross_search_command_contract() -> None:
    with pytest.raises(ValidationError):
        SearchEvalApproval.model_validate(
            {
                "candidate_id": "cand-v6",
                "decision_id": "decision-v6",
                "verdict": "NOTIFY",
                "policy_version": "1.1.0",
                "reason_codes": ["DELAY_NOTIFY_THRESHOLD"],
                "decided_at": "2026-09-15T06:05:00Z",
            }
        )


def test_duffel_test_offer_is_normalized_without_live_or_booking_claim() -> None:
    fixture = json.loads(DUFFEL_FIXTURE.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/air/offer_requests"
        assert request.url.params["return_offers"] == "true"
        assert request.url.params["supplier_timeout"] == "10000"
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert request.headers["Duffel-Version"] == "v2"
        body = json.loads(request.content)
        assert body["data"]["slices"] == [
            {
                "origin": "LHR",
                "destination": "CDG",
                "departure_date": "2026-09-15",
            }
        ]
        assert body["data"]["passengers"] == [
            {"type": "adult"},
            {"type": "adult"},
        ]
        assert body["data"]["cabin_class"] == "economy"
        return httpx.Response(201, json=fixture)

    provider = DuffelFlightSearchProvider(
        token="test-secret", transport=httpx.MockTransport(handler)
    )
    result = run_provider_search(duffel_command(), provider)

    assert result.source_scope == "provider_test_offers"
    assert result.availability_verified is False
    assert result.booking_guaranteed is False
    assert result.booking_authorized is False
    assert len(result.options) == 1
    offer = result.options[0]
    assert offer.option_id == "off_fixture_duffel_001"
    assert offer.price is not None
    assert offer.price.model_dump() == {"amount": "124.50", "currency": "GBP"}
    assert offer.offer_expires_at == "2030-09-15T08:30:00Z"
    assert offer.passenger_count == 2
    assert offer.availability_status == "provider_test_offer"
    assert offer.segments[0].model_dump() == {
        "flight_iata": "ZZ304",
        "origin": "LHR",
        "destination": "CDG",
        "departure_at": "2026-09-15T09:00:00Z",
        "arrival_at": "2026-09-15T10:20:00Z",
    }


def test_duffel_live_mode_is_explicit_but_still_never_authorizes_booking() -> None:
    fixture = json.loads(DUFFEL_FIXTURE.read_text(encoding="utf-8"))
    fixture["data"]["live_mode"] = True
    fixture["data"]["offers"][0]["live_mode"] = True
    provider = DuffelFlightSearchProvider(
        token="live-secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(201, json=fixture)
        ),
    )

    result = run_provider_search(duffel_command(), provider)

    assert result.source_scope == "live_offers"
    assert result.availability_verified is True
    assert result.booking_guaranteed is False
    assert result.booking_authorized is False
    assert result.options[0].availability_status == "live_offer"


def test_duffel_error_is_sanitized_and_does_not_leak_token_or_body() -> None:
    token = "never-print-this-token"
    provider = DuffelFlightSearchProvider(
        token=token,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                401,
                json={
                    "errors": [
                        {
                            "code": "authentication_error",
                            "message": f"bad credential {token}",
                        }
                    ]
                },
            )
        ),
    )

    with pytest.raises(DuffelFlightSearchProviderError) as error:
        provider.search(duffel_command())

    assert str(error.value) == "Duffel HTTP 401: authentication_error"
    assert token not in str(error.value)


def test_expired_duffel_offer_is_rejected_before_presenting_alternatives() -> None:
    fixture = json.loads(DUFFEL_FIXTURE.read_text(encoding="utf-8"))
    provider = DuffelFlightSearchProvider(
        token="test-secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(201, json=fixture)
        ),
    )
    command = duffel_command()
    result = run_provider_search(command, provider)
    expired_result = FlightSearchToolResult.model_validate(
        {
            **result.model_dump(mode="json"),
            "searched_at": "2030-09-15T08:31:00Z",
        }
    )

    alternatives, rejected = rank_feasible_options(command, expired_result)

    assert alternatives == []
    assert rejected == {"OFFER_EXPIRED": 1}
