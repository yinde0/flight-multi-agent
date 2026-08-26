from __future__ import annotations

import copy
import json
import traceback

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from flight_agent.eval_service import commit_evaluation, evaluate_candidate, load_policy
from flight_agent.flight_status import ReplayFlightStatusProvider
from flight_agent.monitoring_contracts import MonitoringPollRequest
from flight_agent.monitoring_flow import run_monitoring_flow
from flight_agent.weather import (
    AirportCoordinateRegistry,
    OpenWeatherMapProvider,
    ReplayWeatherProvider,
    WeatherProviderError,
    classify_weather_risk,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "travel_eval" / "fixtures" / "monitoring"


class MemoryStore:
    def __init__(self) -> None:
        self.flights: dict[str, dict[str, Any]] = {}
        self.weather: dict[str, dict[str, Any]] = {}
        self.candidates: dict[str, dict[str, Any]] = {}
        self.decisions: dict[str, dict[str, Any]] = {}
        self.confirmed: dict[str, dict[str, Any]] = {}
        self.notifications: dict[str, dict[str, Any]] = {}
        self.bands: dict[str, int] = {}

    @staticmethod
    def _key(trip_id: str, leg_id: str) -> str:
        return f"{trip_id}:{leg_id}"

    def get_last_observation(self, trip_id, leg_id):
        return copy.deepcopy(self.flights.get(self._key(trip_id, leg_id)))

    def put_last_observation(self, trip_id, leg_id, observation):
        self.flights[self._key(trip_id, leg_id)] = copy.deepcopy(observation)

    def get_last_weather(self, trip_id, leg_id):
        return copy.deepcopy(self.weather.get(self._key(trip_id, leg_id)))

    def put_last_weather(self, trip_id, leg_id, weather):
        self.weather[self._key(trip_id, leg_id)] = copy.deepcopy(weather)

    def put_candidate(self, candidate):
        self.candidates[candidate["candidate_id"]] = copy.deepcopy(candidate)

    def get_notification(self, decision_id):
        return copy.deepcopy(self.notifications.get(decision_id))

    def put_notification(self, decision_id, notification):
        self.notifications[decision_id] = copy.deepcopy(notification)

    def wait_for_notification(self, decision_id, *, timeout_seconds):
        del timeout_seconds
        return self.get_notification(decision_id)

    def get_decision(self, candidate_id):
        return copy.deepcopy(self.decisions.get(candidate_id))

    def put_decision(self, candidate_id, decision):
        self.decisions[candidate_id] = copy.deepcopy(decision)

    def get_confirmed_event(self, candidate_id):
        return copy.deepcopy(self.confirmed.get(candidate_id))

    def put_confirmed_event(self, candidate_id, event):
        self.confirmed[candidate_id] = copy.deepcopy(event)

    def get_policy_band(self, episode_key):
        return self.bands.get(episode_key)

    def put_policy_band(self, episode_key, band):
        self.bands[episode_key] = band

    def wait_for_decision(self, candidate_id, *, timeout_seconds):
        del timeout_seconds
        return self.get_decision(candidate_id), self.get_confirmed_event(candidate_id)


class InlineEval:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.policy = load_policy()
        self.published: list[dict[str, Any]] = []

    def publish_candidate(self, candidate: dict[str, Any]) -> None:
        self.published.append(copy.deepcopy(candidate))
        decision, confirmed, episode, band, existing = evaluate_candidate(
            candidate, self.store, self.policy
        )
        if not existing:
            commit_evaluation(
                candidate_id=candidate["candidate_id"],
                decision=decision,
                confirmed_event=confirmed,
                episode_key=episode,
                notified_band=band,
                store=self.store,
            )


def _request(replay_key: str = "vertical-04-unit") -> MonitoringPollRequest:
    return MonitoringPollRequest(
        trip_id="trip-v4",
        leg_id="leg-v4",
        flight_iata="NB204",
        flight_date="2026-09-15",
        replay_key=replay_key,
    )


def _decision_view(outcome: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {
        "status": outcome["status"],
        "weather_status": outcome["orchestration"]["weather_evidence"]["status"],
    }
    candidate = outcome.get("candidate")
    decision = outcome.get("decision")
    if candidate and decision:
        view.update(
            {
                "category": candidate["category"],
                "weather_risk_level": candidate["weather_risk_level"],
                "delay_minutes": candidate["delay_minutes"],
                "corroborated": candidate["corroborated_by_weather"],
                "verdict": decision["verdict"],
                "reason_codes": decision["reason_codes"],
                "confirmed": "confirmed_event" in outcome,
            }
        )
    else:
        view["candidate_published"] = bool(candidate)
    return view


def test_seven_poll_weather_corroboration_golden() -> None:
    flight = ReplayFlightStatusProvider(FIXTURES / "vertical_04_flight_timeline.json")
    weather = ReplayWeatherProvider(FIXTURES / "vertical_04_weather_timeline.json")
    store = MemoryStore()
    publisher = InlineEval(store)

    outcomes = [
        run_monitoring_flow(
            _request(),
            flight_status=flight,
            weather=weather,
            store=store,
            publisher=publisher,
        )
        for _ in range(7)
    ]
    expected = json.loads(
        (FIXTURES / "vertical_04_expected.json").read_text(encoding="utf-8")
    )["polls"]

    assert [_decision_view(item) for item in outcomes] == expected
    assert len(publisher.published) == 4
    assert len(store.confirmed) == 1
    assert outcomes[6]["orchestration"]["weather_evidence"] == {
        "status": "unavailable",
        "error_code": "WEATHER_MCP_FAILED",
    }
    assert store.weather["trip-v4:leg-v4"]["observation_id"] == "weather-v4-006"


def test_weather_risk_mapping_is_deterministic() -> None:
    assert classify_weather_risk(800, wind_speed_mps=2, visibility_metres=10000) == (
        "none",
        [],
    )
    assert classify_weather_risk(500, wind_speed_mps=4, visibility_metres=9000) == (
        "low",
        ["LIGHT_PRECIPITATION"],
    )
    assert classify_weather_risk(202, wind_speed_mps=12, visibility_metres=3000) == (
        "severe",
        ["THUNDERSTORM"],
    )


def test_openweather_forecast_is_selected_and_normalized_without_key_leak() -> None:
    captured_url = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        assert request.url.params["appid"] == "secret-weather-key"
        assert request.url.params["units"] == "metric"
        assert request.url.params["lat"] == "51.47"
        assert request.url.params["lon"] == "-0.4543"
        return httpx.Response(
            200,
            json={
                "cod": "200",
                "list": [
                    {
                        "dt_txt": "2026-08-26 09:00:00",
                        "weather": [
                            {"id": 800, "main": "Clear", "description": "clear sky"}
                        ],
                        "pop": 0,
                        "wind": {"speed": 3.0},
                        "visibility": 10000,
                    },
                    {
                        "dt_txt": "2026-08-26 12:00:00",
                        "weather": [
                            {
                                "id": 202,
                                "main": "Thunderstorm",
                                "description": "thunderstorm with heavy rain",
                            }
                        ],
                        "pop": 0.9,
                        "wind": {"speed": 12.0},
                        "visibility": 3000,
                    },
                ],
            },
        )

    provider = OpenWeatherMapProvider(
        api_key="secret-weather-key",
        registry=AirportCoordinateRegistry(ROOT / "travel_eval" / "fixtures" / "airports.json"),
        base_url="https://weather.test/data/2.5",
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 8, 26, 8, tzinfo=timezone.utc),
    )
    observation = provider.get_airport_weather(
        airport="LHR", target_at="2026-08-26T11:20:00Z"
    )

    assert observation.forecast_at == "2026-08-26T12:00:00Z"
    assert observation.risk_level == "severe"
    assert observation.alerts == ["THUNDERSTORM"]
    assert "secret-weather-key" not in observation.model_dump_json()
    assert "appid=secret-weather-key" in captured_url


def test_openweather_error_traceback_is_sanitized() -> None:
    provider = OpenWeatherMapProvider(
        api_key="secret-must-stay-hidden",
        base_url="https://weather.test/data/2.5",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"cod": 401})
        ),
    )
    try:
        provider.get_airport_weather(
            airport="LHR", target_at="2026-08-26T11:20:00Z"
        )
    except WeatherProviderError as error:
        rendered = "".join(traceback.format_exception(error))
    else:
        raise AssertionError("Expected a sanitized provider error")

    assert "HTTP 401: 401" in rendered
    assert "secret-must-stay-hidden" not in rendered
    assert "appid=" not in rendered
