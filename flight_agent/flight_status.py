from __future__ import annotations

import hashlib
import json
import os
import threading

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from flight_agent.monitoring_contracts import ProviderFlightObservation


DEFAULT_AVIATIONSTACK_BASE_URL = "https://api.aviationstack.com/v1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPLAY_FIXTURE = (
    ROOT / "travel_eval" / "fixtures" / "monitoring" / "vertical_03_timeline.json"
)


class FlightStatusProviderError(RuntimeError):
    """The configured flight-status provider could not return a usable record."""


class FlightStatusProvider(Protocol):
    def get_flight_status(
        self,
        *,
        flight_iata: str,
        flight_date: str,
        replay_key: str | None = None,
    ) -> ProviderFlightObservation: ...


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


def _optional_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return _format_utc(_parse_timestamp(value))


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


class ReplayFlightStatusProvider:
    """Deterministic call-by-call provider used only by the Docker golden suite."""

    def __init__(self, fixture_path: Path = DEFAULT_REPLAY_FIXTURE) -> None:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self._flight_iata = str(fixture["flight_iata"]).upper()
        self._flight_date = str(fixture["flight_date"])
        self._observations = fixture["observations"]
        self._cursors: dict[str, int] = {}
        self._lock = threading.Lock()

    def get_flight_status(
        self,
        *,
        flight_iata: str,
        flight_date: str,
        replay_key: str | None = None,
    ) -> ProviderFlightObservation:
        if flight_iata.upper() != self._flight_iata or flight_date != self._flight_date:
            raise FlightStatusProviderError("No replay timeline for requested flight")
        if not replay_key:
            raise FlightStatusProviderError("Replay provider requires replay_key")

        with self._lock:
            index = self._cursors.get(replay_key, 0)
            if index >= len(self._observations):
                index = len(self._observations) - 1
            self._cursors[replay_key] = index + 1
        return ProviderFlightObservation.model_validate(
            deepcopy(self._observations[index])
        )


class AviationStackFlightStatusProvider:
    """Normalize AviationStack's flights response into the canonical observation."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_AVIATIONSTACK_BASE_URL,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def get_flight_status(
        self,
        *,
        flight_iata: str,
        flight_date: str,
        replay_key: str | None = None,
    ) -> ProviderFlightObservation:
        del replay_key
        if not self._api_key:
            raise FlightStatusProviderError(
                "AviationStack_API_KEY is required for the live provider"
            )
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.get(
                    f"{self._base_url}/flights",
                    params={
                        "access_key": self._api_key,
                        "flight_iata": flight_iata,
                        "flight_date": flight_date,
                        "limit": 10,
                    },
                )
        except httpx.HTTPError:
            # httpx exceptions may embed the full query string, including the
            # access key. Never chain them into service logs or MCP errors.
            raise FlightStatusProviderError("AviationStack request failed") from None

        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        if response.is_error:
            provider_code = "provider_error"
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                raw_code = str(payload["error"].get("code", provider_code))
                provider_code = "".join(
                    character
                    for character in raw_code
                    if character.isalnum() or character in {"_", "-"}
                )[:80] or provider_code
            raise FlightStatusProviderError(
                f"AviationStack HTTP {response.status_code}: {provider_code}"
            ) from None

        if not isinstance(payload, dict):
            raise FlightStatusProviderError("AviationStack returned invalid JSON")
        if isinstance(payload.get("error"), dict):
            code = payload["error"].get("code", "provider_error")
            raise FlightStatusProviderError(f"AviationStack error: {code}")
        records = payload.get("data")
        if not isinstance(records, list) or not records:
            raise FlightStatusProviderError("AviationStack returned no matching flight")

        selected = next(
            (
                item
                for item in records
                if isinstance(item, dict)
                and str(item.get("flight_date")) == flight_date
                and str((item.get("flight") or {}).get("iata", "")).upper()
                == flight_iata.upper()
            ),
            None,
        )
        if not isinstance(selected, dict):
            raise FlightStatusProviderError(
                "AviationStack returned no exact flight/date match"
            )
        return self._normalize(selected, flight_iata)

    @staticmethod
    def _normalize(
        record: dict[str, Any], flight_iata: str
    ) -> ProviderFlightObservation:
        departure = record.get("departure") or {}
        arrival = record.get("arrival") or {}
        if not isinstance(departure, dict) or not isinstance(arrival, dict):
            raise FlightStatusProviderError("AviationStack movements are missing")

        departure_scheduled = _optional_timestamp(departure.get("scheduled"))
        arrival_scheduled = _optional_timestamp(arrival.get("scheduled"))
        departure_airport = str(departure.get("iata") or "").upper()
        if not departure_scheduled or not arrival_scheduled:
            raise FlightStatusProviderError("AviationStack scheduled times are missing")
        if len(departure_airport) != 3:
            raise FlightStatusProviderError("AviationStack departure IATA is missing")

        now = _now_utc()
        live = record.get("live") or {}
        updated = live.get("updated") if isinstance(live, dict) else None
        source_time = _parse_timestamp(updated) if isinstance(updated, str) else now
        status = str(record.get("flight_status") or "unknown").lower()
        status = {
            "scheduled": "scheduled",
            "active": "active",
            "landed": "landed",
            "cancelled": "cancelled",
            "diverted": "diverted",
        }.get(status, "unknown")

        identity = json.dumps(record, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        observed_at = _format_utc(now)
        return ProviderFlightObservation(
            observation_id=f"obs-aviationstack-{digest}",
            observed_at=observed_at,
            source="aviationstack",
            source_event_time=_format_utc(source_time),
            status=status,
            departure={
                "scheduled_at": departure_scheduled,
                "estimated_at": _optional_timestamp(departure.get("estimated"))
                or departure_scheduled,
                "actual_at": _optional_timestamp(departure.get("actual")),
                "terminal": _optional_string(departure.get("terminal")),
                "gate": _optional_string(departure.get("gate")),
            },
            arrival={
                "scheduled_at": arrival_scheduled,
                "estimated_at": _optional_timestamp(arrival.get("estimated"))
                or arrival_scheduled,
                "actual_at": _optional_timestamp(arrival.get("actual")),
                "terminal": _optional_string(arrival.get("terminal")),
                "gate": _optional_string(arrival.get("gate")),
            },
            weather={
                "airport": departure_airport,
                "valid_at": departure_scheduled,
                "risk_level": "none",
                "alerts": [],
            },
            data_freshness_seconds=max(0, int((now - source_time).total_seconds())),
            confidence=0.95 if updated else 0.85,
        )


def provider_from_environment() -> FlightStatusProvider:
    provider_name = os.getenv("FLIGHT_STATUS_PROVIDER", "aviationstack").lower()
    if provider_name == "replay":
        fixture_path = Path(
            os.getenv("FLIGHT_STATUS_REPLAY_FIXTURE", str(DEFAULT_REPLAY_FIXTURE))
        )
        return ReplayFlightStatusProvider(fixture_path)
    if provider_name != "aviationstack":
        raise FlightStatusProviderError(
            f"Unsupported FLIGHT_STATUS_PROVIDER: {provider_name}"
        )
    return AviationStackFlightStatusProvider(
        api_key=os.getenv("AviationStack_API_KEY", ""),
        base_url=os.getenv(
            "AVIATIONSTACK_BASE_URL", DEFAULT_AVIATIONSTACK_BASE_URL
        ),
        timeout_seconds=float(os.getenv("AVIATIONSTACK_TIMEOUT_SECONDS", "30")),
    )
