from __future__ import annotations

import copy
import json
import traceback
import unittest
import uuid

from pathlib import Path
from typing import Any

import httpx

from fastapi.testclient import TestClient

from flight_agent.api import create_api_app
from flight_agent.eval_service import commit_evaluation, evaluate_candidate, load_policy
from flight_agent.flight_status import (
    AviationStackFlightStatusProvider,
    FlightStatusProviderError,
    ReplayFlightStatusProvider,
)
from flight_agent.monitoring_a2a import create_monitoring_agent_app
from flight_agent.monitoring_contracts import (
    MonitoringPollOutcome,
    MonitoringPollRequest,
    ProviderFlightObservation,
)
from flight_agent.monitoring_flow import run_monitoring_flow


ROOT = Path(__file__).resolve().parents[1]
MONITORING_FIXTURES = ROOT / "travel_eval" / "fixtures" / "monitoring"


class InMemoryMonitoringStore:
    def __init__(self) -> None:
        self.last_observations: dict[str, dict[str, Any]] = {}
        self.last_weather: dict[str, dict[str, Any]] = {}
        self.candidates: dict[str, dict[str, Any]] = {}
        self.decisions: dict[str, dict[str, Any]] = {}
        self.confirmed_events: dict[str, dict[str, Any]] = {}
        self.notifications: dict[str, dict[str, Any]] = {}
        self.policy_bands: dict[str, int] = {}

    @staticmethod
    def _leg_key(trip_id: str, leg_id: str) -> str:
        return f"{trip_id}:{leg_id}"

    def get_last_observation(
        self, trip_id: str, leg_id: str
    ) -> dict[str, Any] | None:
        return copy.deepcopy(
            self.last_observations.get(self._leg_key(trip_id, leg_id))
        )

    def put_last_observation(
        self, trip_id: str, leg_id: str, observation: dict[str, Any]
    ) -> None:
        self.last_observations[self._leg_key(trip_id, leg_id)] = copy.deepcopy(
            observation
        )

    def get_last_weather(
        self, trip_id: str, leg_id: str
    ) -> dict[str, Any] | None:
        return copy.deepcopy(self.last_weather.get(self._leg_key(trip_id, leg_id)))

    def put_last_weather(
        self, trip_id: str, leg_id: str, weather: dict[str, Any]
    ) -> None:
        self.last_weather[self._leg_key(trip_id, leg_id)] = copy.deepcopy(weather)

    def put_candidate(self, candidate: dict[str, Any]) -> None:
        self.candidates[candidate["candidate_id"]] = copy.deepcopy(candidate)

    def get_notification(self, decision_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.notifications.get(decision_id))

    def put_notification(
        self, decision_id: str, notification: dict[str, Any]
    ) -> None:
        self.notifications[decision_id] = copy.deepcopy(notification)

    def wait_for_notification(
        self, decision_id: str, *, timeout_seconds: float
    ) -> dict[str, Any] | None:
        del timeout_seconds
        return self.get_notification(decision_id)

    def get_decision(self, candidate_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.decisions.get(candidate_id))

    def put_decision(self, candidate_id: str, decision: dict[str, Any]) -> None:
        self.decisions[candidate_id] = copy.deepcopy(decision)

    def get_confirmed_event(self, candidate_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.confirmed_events.get(candidate_id))

    def put_confirmed_event(
        self, candidate_id: str, event: dict[str, Any]
    ) -> None:
        self.confirmed_events[candidate_id] = copy.deepcopy(event)

    def get_policy_band(self, episode_key: str) -> int | None:
        return self.policy_bands.get(episode_key)

    def put_policy_band(self, episode_key: str, band: int) -> None:
        self.policy_bands[episode_key] = band

    def wait_for_decision(
        self, candidate_id: str, *, timeout_seconds: float
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        del timeout_seconds
        return (
            self.get_decision(candidate_id),
            self.get_confirmed_event(candidate_id),
        )


class InlineEvalPublisher:
    """Synchronous test double for the NATS boundary and Eval Agent consumer."""

    def __init__(self, store: InMemoryMonitoringStore) -> None:
        self.store = store
        self.policy = load_policy()
        self.published: list[dict[str, Any]] = []

    def publish_candidate(self, candidate: dict[str, Any]) -> None:
        self.published.append(copy.deepcopy(candidate))
        (
            decision,
            confirmed_event,
            episode_key,
            notified_band,
            already_processed,
        ) = evaluate_candidate(candidate, self.store, self.policy)
        if not already_processed:
            commit_evaluation(
                candidate_id=candidate["candidate_id"],
                decision=decision,
                confirmed_event=confirmed_event,
                episode_key=episode_key,
                notified_band=notified_band,
                store=self.store,
            )


class FailingFlightStatusGateway:
    def get_flight_status(self, **kwargs) -> ProviderFlightObservation:
        del kwargs
        raise RuntimeError("simulated MCP outage")


def request_for(replay_key: str = "golden-unit") -> MonitoringPollRequest:
    return MonitoringPollRequest(
        trip_id="trip-v3",
        leg_id="leg-v3",
        flight_iata="NB204",
        flight_date="2026-09-15",
        replay_key=replay_key,
    )


def decision_view(outcome: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {"status": outcome["status"]}
    candidate = outcome.get("candidate")
    decision = outcome.get("decision")
    if candidate and decision:
        view.update(
            {
                "category": candidate["category"],
                "delay_minutes": candidate["delay_minutes"],
                "verdict": decision["verdict"],
                "reason_codes": decision["reason_codes"],
                "confirmed": "confirmed_event" in outcome,
            }
        )
    else:
        view["candidate_published"] = bool(candidate)
    return view


class MonitoringGoldenTests(unittest.TestCase):
    def test_six_poll_timeline_matches_decision_golden(self):
        provider = ReplayFlightStatusProvider(
            MONITORING_FIXTURES / "vertical_03_timeline.json"
        )
        store = InMemoryMonitoringStore()
        publisher = InlineEvalPublisher(store)

        outcomes = [
            run_monitoring_flow(
                request_for(),
                flight_status=provider,
                store=store,
                publisher=publisher,
            )
            for _ in range(6)
        ]
        expected = json.loads(
            (MONITORING_FIXTURES / "vertical_03_expected.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual([decision_view(item) for item in outcomes], expected["polls"])
        self.assertEqual(len(publisher.published), 3)
        self.assertEqual(len(store.confirmed_events), 1)
        self.assertEqual(
            next(iter(store.confirmed_events.values()))["verdict"], "NOTIFY"
        )
        self.assertEqual(
            outcomes[4]["candidate"]["candidate_id"],
            "cand-v3-v3-v3-005",
        )

    def test_durable_state_is_used_by_a_new_flow_instance(self):
        provider = ReplayFlightStatusProvider(
            MONITORING_FIXTURES / "vertical_03_timeline.json"
        )
        store = InMemoryMonitoringStore()
        publisher = InlineEvalPublisher(store)
        first = run_monitoring_flow(
            request_for("restart-case"),
            flight_status=provider,
            store=store,
            publisher=publisher,
        )
        second = run_monitoring_flow(
            request_for("restart-case"),
            flight_status=provider,
            store=store,
            publisher=publisher,
        )
        self.assertEqual(first["status"], "baseline_stored")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(publisher.published, [])

    def test_provider_failure_fails_closed_without_candidate(self):
        store = InMemoryMonitoringStore()
        publisher = InlineEvalPublisher(store)
        outcome = run_monitoring_flow(
            request_for("failure-case"),
            flight_status=FailingFlightStatusGateway(),
            store=store,
            publisher=publisher,
        )
        self.assertEqual(outcome["status"], "poll_failed")
        self.assertEqual(outcome["error_code"], "FLIGHT_STATUS_MCP_FAILED")
        self.assertNotIn("candidate", outcome)
        self.assertEqual(store.last_observations, {})
        self.assertEqual(publisher.published, [])

    def test_same_leg_id_on_different_trips_has_independent_state(self):
        provider = ReplayFlightStatusProvider(
            MONITORING_FIXTURES / "vertical_03_timeline.json"
        )
        store = InMemoryMonitoringStore()
        publisher = InlineEvalPublisher(store)
        first_trip = request_for("trip-one-replay")
        second_trip = first_trip.model_copy(
            update={"trip_id": "trip-other", "replay_key": "trip-two-replay"}
        )

        first = run_monitoring_flow(
            first_trip,
            flight_status=provider,
            store=store,
            publisher=publisher,
        )
        second = run_monitoring_flow(
            second_trip,
            flight_status=provider,
            store=store,
            publisher=publisher,
        )

        self.assertEqual(first["status"], "baseline_stored")
        self.assertEqual(second["status"], "baseline_stored")
        self.assertEqual(len(store.last_observations), 2)


class AviationStackAdapterTests(unittest.TestCase):
    def test_live_shape_is_normalized_without_exposing_the_api_key(self):
        captured_url = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_url
            captured_url = str(request.url)
            self.assertEqual(request.url.params["access_key"], "secret-test-key")
            self.assertEqual(request.url.params["flight_iata"], "NB204")
            self.assertEqual(request.url.params["limit"], "100")
            self.assertNotIn("flight_date", request.url.params)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "flight_date": "2026-09-15",
                            "flight_status": "active",
                            "flight": {"iata": "NB204"},
                            "departure": {
                                "iata": "LHR",
                                "scheduled": "2026-09-15T08:20:00+00:00",
                                "estimated": "2026-09-15T08:50:00+00:00",
                                "actual": None,
                                "terminal": "5",
                                "gate": "A12",
                            },
                            "arrival": {
                                "iata": "CDG",
                                "scheduled": "2026-09-15T09:35:00+00:00",
                                "estimated": "2026-09-15T10:05:00+00:00",
                                "actual": None,
                                "terminal": "1",
                                "gate": None,
                            },
                            "live": {"updated": "2026-09-15T08:25:00+00:00"},
                        }
                    ]
                },
            )

        provider = AviationStackFlightStatusProvider(
            api_key="secret-test-key",
            base_url="https://aviation.test/v1",
            transport=httpx.MockTransport(handler),
        )
        observation = provider.get_flight_status(
            flight_iata="NB204", flight_date="2026-09-15"
        )
        dumped = observation.model_dump(mode="json")
        self.assertEqual(observation.status, "active")
        self.assertEqual(observation.departure.gate, "A12")
        self.assertEqual(observation.departure.estimated_at, "2026-09-15T08:50:00Z")
        self.assertNotIn("secret-test-key", json.dumps(dumped))
        self.assertIn("access_key=secret-test-key", captured_url)

    def test_discovery_uses_unfiltered_feed_and_selects_a_real_record(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["access_key"], "secret-test-key")
            self.assertEqual(request.url.params["limit"], "10")
            self.assertEqual(request.url.params["offset"], "0")
            self.assertNotIn("flight_iata", request.url.params)
            self.assertNotIn("flight_date", request.url.params)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"flight_date": "2026-09-15", "flight": {"iata": None}},
                        {
                            "flight_date": "2026-09-15",
                            "flight_status": "scheduled",
                            "flight": {"iata": "NB204"},
                            "departure": {
                                "iata": "LHR",
                                "scheduled": "2026-09-15T08:20:00+00:00",
                                "estimated": "2026-09-15T08:20:00+00:00",
                                "actual": None,
                                "terminal": "5",
                                "gate": "A10",
                            },
                            "arrival": {
                                "iata": "AMS",
                                "scheduled": "2026-09-15T09:35:00+00:00",
                                "estimated": "2026-09-15T09:35:00+00:00",
                                "actual": None,
                                "terminal": "1",
                                "gate": None,
                            },
                            "live": None,
                        },
                    ]
                },
            )

        provider = AviationStackFlightStatusProvider(
            api_key="secret-test-key",
            base_url="https://aviation.test/v1",
            transport=httpx.MockTransport(handler),
        )
        sample = provider.discover_live_flight_sample(limit=10)

        self.assertEqual(sample.flight_iata, "NB204")
        self.assertEqual(sample.flight_date, "2026-09-15")
        self.assertEqual(sample.origin, "LHR")
        self.assertEqual(sample.destination, "AMS")
        self.assertEqual(sample.observation.status, "scheduled")
        self.assertNotIn("secret-test-key", sample.model_dump_json())

    def test_missing_api_key_fails_before_network(self):
        provider = AviationStackFlightStatusProvider(
            api_key="",
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    AssertionError("network must not be called")
                )
            ),
        )
        with self.assertRaises(FlightStatusProviderError):
            provider.get_flight_status(
                flight_iata="NB204", flight_date="2026-09-15"
            )

    def test_http_failure_traceback_does_not_expose_api_key_or_request_url(self):
        provider = AviationStackFlightStatusProvider(
            api_key="secret-must-stay-hidden",
            base_url="https://aviation.test/v1",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    403,
                    json={"error": {"code": "https_access_restricted"}},
                )
            ),
        )
        try:
            provider.get_flight_status(
                flight_iata="NB204", flight_date="2026-09-15"
            )
        except FlightStatusProviderError as error:
            rendered = "".join(traceback.format_exception(error))
        else:
            self.fail("Expected a sanitized provider error")

        self.assertIn("HTTP 403: https_access_restricted", rendered)
        self.assertNotIn("secret-must-stay-hidden", rendered)
        self.assertNotIn("access_key=", rendered)


class FakeMonitoringGateway:
    def __init__(self) -> None:
        self.calls: list[MonitoringPollRequest] = []

    async def poll(self, request: MonitoringPollRequest) -> MonitoringPollOutcome:
        self.calls.append(request)
        return MonitoringPollOutcome(
            status="baseline_stored",
            request=request,
            observation={"observation_id": "obs-api"},
            orchestration={"framework": "fake-test"},
        )


class MonitoringBoundaryTests(unittest.TestCase):
    def test_public_api_forwards_to_monitoring_agent_gateway(self):
        gateway = FakeMonitoringGateway()
        client = TestClient(create_api_app(monitoring_gateway=gateway))
        response = client.post(
            "/v1/monitoring/poll", json=request_for("api-case").model_dump()
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "baseline_stored")
        self.assertEqual(len(gateway.calls), 1)

    def test_a2a_monitoring_agent_returns_structured_artifact(self):
        provider = ReplayFlightStatusProvider(
            MONITORING_FIXTURES / "vertical_03_timeline.json"
        )
        store = InMemoryMonitoringStore()
        publisher = InlineEvalPublisher(store)
        client = TestClient(
            create_monitoring_agent_app(
                "http://monitor-agent.test",
                flight_status=provider,
                store=store,
                publisher=publisher,
            )
        )
        card = client.get("/.well-known/agent-card.json").json()
        self.assertIn("poll_flight_status", {item["id"] for item in card["skills"]})

        request_id = str(uuid.uuid4())
        response = client.post(
            "/a2a/jsonrpc",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": str(uuid.uuid4()),
                        "contextId": str(uuid.uuid4()),
                        "role": "ROLE_USER",
                        "parts": [{"data": request_for("a2a-case").model_dump()}],
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        envelope = response.json()
        self.assertEqual(envelope["id"], request_id)
        outcome = envelope["result"]["task"]["artifacts"][0]["parts"][0]["data"]
        self.assertEqual(outcome["status"], "baseline_stored")


if __name__ == "__main__":
    unittest.main()
