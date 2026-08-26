from __future__ import annotations

import hashlib
import json
import os
import threading

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from pydantic import ValidationError

from flight_agent.monitoring_contracts import ProviderWeatherObservation


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AIRPORT_FIXTURE = ROOT / "travel_eval" / "fixtures" / "airports.json"
DEFAULT_WEATHER_REPLAY_FIXTURE = (
    ROOT / "travel_eval" / "fixtures" / "monitoring" / "vertical_03_weather_timeline.json"
)
DEFAULT_OPENWEATHER_BASE_URL = "https://api.openweathermap.org/data/2.5"


class WeatherProviderError(RuntimeError):
    """Safe provider error that never contains an API key or request URL."""


class WeatherProvider(Protocol):
    def get_airport_weather(
        self,
        *,
        airport: str,
        target_at: str,
        replay_key: str | None = None,
    ) -> ProviderWeatherObservation: ...


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AirportCoordinateRegistry:
    """Resolve IATA codes through a versioned, deterministic local fixture."""

    def __init__(self, fixture_path: Path = DEFAULT_AIRPORT_FIXTURE) -> None:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        airports = payload.get("airports")
        if not isinstance(airports, dict):
            raise WeatherProviderError("Airport coordinate fixture is invalid")
        self._airports = airports

    def coordinates(self, airport: str) -> tuple[float, float]:
        code = airport.strip().upper()
        location = self._airports.get(code)
        if not isinstance(location, dict):
            raise WeatherProviderError(f"No coordinates configured for airport {code}")
        try:
            return float(location["latitude"]), float(location["longitude"])
        except (KeyError, TypeError, ValueError):
            raise WeatherProviderError(
                f"Coordinates are invalid for airport {code}"
            ) from None


def classify_weather_risk(
    condition_code: int,
    *,
    wind_speed_mps: float,
    visibility_metres: int | None,
) -> tuple[str, list[str]]:
    """Map provider measurements to stable, explainable operational evidence."""
    risk = "none"
    factors: list[str] = []

    if 200 <= condition_code < 300:
        risk = "severe"
        factors.append("THUNDERSTORM")
    elif condition_code in {502, 503, 504, 511, 522, 602, 622, 762, 771, 781}:
        risk = "severe"
        factors.append("EXTREME_CONDITION")
    elif condition_code in {501, 601, 611, 612, 613, 615, 616, 621, 741}:
        risk = "high"
        factors.append("HAZARDOUS_CONDITION")
    elif 300 <= condition_code < 400 or condition_code in {500, 520}:
        risk = "low"
        factors.append("LIGHT_PRECIPITATION")
    elif 521 == condition_code or 600 == condition_code or 700 <= condition_code < 800:
        risk = "moderate"
        factors.append("REDUCED_OPERATING_CONDITIONS")

    rank = {"none": 0, "low": 1, "moderate": 2, "high": 3, "severe": 4}
    if wind_speed_mps >= 25:
        risk = "severe"
        factors.append("EXTREME_WIND")
    elif wind_speed_mps >= 15 and rank[risk] < rank["high"]:
        risk = "high"
        factors.append("HIGH_WIND")

    if visibility_metres is not None and visibility_metres < 500:
        risk = "severe"
        factors.append("VERY_LOW_VISIBILITY")
    elif (
        visibility_metres is not None
        and visibility_metres < 1500
        and rank[risk] < rank["high"]
    ):
        risk = "high"
        factors.append("LOW_VISIBILITY")
    return risk, factors


class ReplayWeatherProvider:
    """Deterministic call-by-call weather provider for golden Docker tests."""

    def __init__(self, fixture_path: Path = DEFAULT_WEATHER_REPLAY_FIXTURE) -> None:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self._airport = str(fixture["airport"]).upper()
        self._observations = fixture["observations"]
        self._cursors: dict[str, int] = {}
        self._lock = threading.Lock()

    def get_airport_weather(
        self,
        *,
        airport: str,
        target_at: str,
        replay_key: str | None = None,
    ) -> ProviderWeatherObservation:
        del target_at
        if airport.upper() != self._airport:
            raise WeatherProviderError("No replay timeline for requested airport")
        if not replay_key:
            raise WeatherProviderError("Replay provider requires replay_key")
        with self._lock:
            index = self._cursors.get(replay_key, 0)
            if index >= len(self._observations):
                index = len(self._observations) - 1
            self._cursors[replay_key] = index + 1
        item = deepcopy(self._observations[index])
        if isinstance(item, dict) and item.get("error"):
            raise WeatherProviderError("Replay weather provider failure")
        try:
            return ProviderWeatherObservation.model_validate(item)
        except ValidationError:
            raise WeatherProviderError("Replay weather observation is invalid") from None


class OpenWeatherMapProvider:
    """Normalize OpenWeather's 5-day/3-hour forecast into one airport forecast."""

    def __init__(
        self,
        *,
        api_key: str,
        registry: AirportCoordinateRegistry | None = None,
        base_url: str = DEFAULT_OPENWEATHER_BASE_URL,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._api_key = api_key.strip()
        self._registry = registry or AirportCoordinateRegistry()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._clock = clock

    @staticmethod
    def _safe_provider_code(value: Any) -> str:
        raw = str(value or "provider_error")
        return "".join(
            character
            for character in raw
            if character.isalnum() or character in {"_", "-"}
        )[:80] or "provider_error"

    def get_airport_weather(
        self,
        *,
        airport: str,
        target_at: str,
        replay_key: str | None = None,
    ) -> ProviderWeatherObservation:
        del replay_key
        if not self._api_key:
            raise WeatherProviderError(
                "OpenWeatherMap_API_KEY is required for the live provider"
            )
        code = airport.strip().upper()
        latitude, longitude = self._registry.coordinates(code)
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.get(
                    f"{self._base_url}/forecast",
                    params={
                        "lat": latitude,
                        "lon": longitude,
                        "appid": self._api_key,
                        "units": "metric",
                    },
                )
        except httpx.HTTPError:
            raise WeatherProviderError("OpenWeather request failed") from None

        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        if response.is_error:
            provider_code = self._safe_provider_code(
                payload.get("cod") if isinstance(payload, dict) else None
            )
            raise WeatherProviderError(
                f"OpenWeather HTTP {response.status_code}: {provider_code}"
            ) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("list"), list):
            raise WeatherProviderError("OpenWeather returned invalid JSON")

        target = _parse_timestamp(target_at)
        options = [item for item in payload["list"] if isinstance(item, dict)]
        try:
            selected = min(
                options,
                key=lambda item: abs(
                    _parse_timestamp(str(item["dt_txt"])) - target
                ),
            )
            forecast_at = _parse_timestamp(str(selected["dt_txt"]))
        except (KeyError, TypeError, ValueError):
            raise WeatherProviderError(
                "OpenWeather returned no usable forecast steps"
            ) from None

        distance_seconds = abs((forecast_at - target).total_seconds())
        if distance_seconds > 3 * 60 * 60:
            raise WeatherProviderError(
                "OpenWeather returned no forecast near the target time"
            )
        return self._normalize(
            selected,
            airport=code,
            latitude=latitude,
            longitude=longitude,
            target_at=target,
            forecast_at=forecast_at,
            distance_seconds=distance_seconds,
        )

    def _normalize(
        self,
        item: dict[str, Any],
        *,
        airport: str,
        latitude: float,
        longitude: float,
        target_at: datetime,
        forecast_at: datetime,
        distance_seconds: float,
    ) -> ProviderWeatherObservation:
        try:
            weather = item["weather"][0]
            condition_code = int(weather["id"])
            condition = str(weather["main"])
            description = str(weather["description"])
            wind_speed = max(0.0, float((item.get("wind") or {}).get("speed", 0)))
            visibility_raw = item.get("visibility")
            visibility = int(visibility_raw) if visibility_raw is not None else None
            precipitation_probability = min(1.0, max(0.0, float(item.get("pop", 0))))
        except (KeyError, IndexError, TypeError, ValueError):
            raise WeatherProviderError("OpenWeather forecast step is malformed") from None

        risk, factors = classify_weather_risk(
            condition_code,
            wind_speed_mps=wind_speed,
            visibility_metres=visibility,
        )
        identity = json.dumps(item, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(
            f"{airport}:{_format_utc(target_at)}:{identity}".encode("utf-8")
        ).hexdigest()[:16]
        confidence = 0.95 if distance_seconds <= 90 * 60 else 0.85
        return ProviderWeatherObservation(
            observation_id=f"weather-openweather-{digest}",
            observed_at=_format_utc(self._clock()),
            source="openweathermap",
            airport=airport,
            latitude=latitude,
            longitude=longitude,
            target_at=_format_utc(target_at),
            forecast_at=_format_utc(forecast_at),
            condition_code=condition_code,
            condition=condition,
            description=description,
            risk_level=risk,
            alerts=factors,
            precipitation_probability=precipitation_probability,
            wind_speed_mps=wind_speed,
            visibility_metres=visibility,
            confidence=confidence,
        )


class NeutralWeatherGateway:
    """In-process compatibility gateway for slice-03 unit tests."""

    def get_airport_weather(
        self,
        *,
        airport: str,
        target_at: str,
        replay_key: str | None = None,
    ) -> ProviderWeatherObservation:
        del replay_key
        return ProviderWeatherObservation(
            observation_id=f"weather-neutral-{airport}-{target_at}",
            observed_at=target_at,
            source="neutral-test-weather",
            airport=airport,
            latitude=0,
            longitude=0,
            target_at=target_at,
            forecast_at=target_at,
            condition_code=800,
            condition="Clear",
            description="clear sky",
            risk_level="none",
            alerts=[],
            precipitation_probability=0,
            wind_speed_mps=0,
            visibility_metres=10000,
            confidence=1,
        )


def provider_from_environment() -> WeatherProvider:
    provider_name = os.getenv("WEATHER_PROVIDER", "openweathermap").lower()
    if provider_name == "replay":
        return ReplayWeatherProvider(
            Path(
                os.getenv(
                    "WEATHER_REPLAY_FIXTURE",
                    str(DEFAULT_WEATHER_REPLAY_FIXTURE),
                )
            )
        )
    if provider_name != "openweathermap":
        raise WeatherProviderError(f"Unsupported WEATHER_PROVIDER: {provider_name}")
    return OpenWeatherMapProvider(
        api_key=os.getenv("OpenWeatherMap_API_KEY", ""),
        registry=AirportCoordinateRegistry(
            Path(os.getenv("AIRPORT_COORDINATE_FIXTURE", str(DEFAULT_AIRPORT_FIXTURE)))
        ),
        base_url=os.getenv("OPENWEATHER_BASE_URL", DEFAULT_OPENWEATHER_BASE_URL),
        timeout_seconds=float(os.getenv("OPENWEATHER_TIMEOUT_SECONDS", "30")),
    )
