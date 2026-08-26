from __future__ import annotations

import json

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from flight_agent.flight_search_contracts import (
    FlightSearchCommand,
    FlightSearchToolResult,
    ProviderFlightOption,
    RankedFlightOption,
)
from travel_eval.clock import parse_timestamp


class FlightSearchProvider(Protocol):
    name: str

    def search(self, command: FlightSearchCommand) -> "ProviderSearchBatch": ...


@dataclass(frozen=True)
class ProviderSearchBatch:
    source_scope: str
    options: list[ProviderFlightOption]
    availability_verified: bool = False


class DisabledFlightSearchProvider:
    """Fail closed until a real schedule/shopping provider is configured."""

    name = "disabled"

    def search(self, command: FlightSearchCommand) -> ProviderSearchBatch:
        del command
        raise RuntimeError("Flight search provider is disabled")


class ReplayFlightSearchProvider:
    """Deterministic schedule fixture used by the vertical evaluation."""

    name = "replay"

    def __init__(self, fixture_path: str | Path) -> None:
        payload = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        options = payload.get("options")
        if not isinstance(options, list):
            raise ValueError("Flight search replay fixture must contain options")
        self._options = [ProviderFlightOption.model_validate(item) for item in options]

    def search(self, command: FlightSearchCommand) -> ProviderSearchBatch:
        del command
        return ProviderSearchBatch(
            source_scope="synthetic_replay",
            options=[option.model_copy(deep=True) for option in self._options],
        )


class DuffelFlightSearchProviderError(RuntimeError):
    """A safe Duffel failure that never includes credentials or response bodies."""


class DuffelFlightSearchProvider:
    """Create Duffel offer requests and normalize expiring priced offers."""

    name = "duffel"

    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://api.duffel.com",
        timeout_seconds: float = 30.0,
        supplier_timeout_ms: int = 10_000,
        maximum_offers: int = 100,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._supplier_timeout_ms = max(2_000, min(60_000, supplier_timeout_ms))
        self._maximum_offers = max(1, min(200, maximum_offers))
        self._transport = transport

    @staticmethod
    def _safe_code(value: Any) -> str:
        raw = str(value or "provider_error")
        return "".join(
            character
            for character in raw
            if character.isalnum() or character in {"_", "-"}
        )[:80] or "provider_error"

    @staticmethod
    def _utc_from_local(value: Any, timezone_name: Any) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("Duffel segment time is missing")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            if not isinstance(timezone_name, str) or not timezone_name:
                raise ValueError("Duffel airport timezone is missing")
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _utc_timestamp(value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("Duffel timestamp is missing")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Duffel timestamp has no timezone")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def _normalize_offer(
        cls,
        offer: dict[str, Any],
        *,
        passenger_count: int,
    ) -> ProviderFlightOption:
        raw_segments = [
            segment
            for slice_item in offer["slices"]
            for segment in slice_item["segments"]
        ]
        segments = []
        for segment in raw_segments:
            marketing = segment.get("marketing_carrier") or {}
            operating = segment.get("operating_carrier") or {}
            carrier = str(
                marketing.get("iata_code") or operating.get("iata_code") or ""
            ).upper()
            number = str(
                segment.get("marketing_carrier_flight_number")
                or segment.get("operating_carrier_flight_number")
                or ""
            ).upper()
            flight_iata = "".join(
                character for character in f"{carrier}{number}" if character.isalnum()
            )
            origin = segment["origin"]
            destination = segment["destination"]
            segments.append(
                {
                    "flight_iata": flight_iata,
                    "origin": str(origin["iata_code"]).upper(),
                    "destination": str(destination["iata_code"]).upper(),
                    "departure_at": cls._utc_from_local(
                        segment["departing_at"], origin.get("time_zone")
                    ),
                    "arrival_at": cls._utc_from_local(
                        segment["arriving_at"], destination.get("time_zone")
                    ),
                }
            )
        owner = offer.get("owner") or {}
        live_mode = bool(offer.get("live_mode"))
        return ProviderFlightOption(
            option_id=str(offer["id"]),
            segments=segments,
            price={
                "amount": str(offer["total_amount"]),
                "currency": str(offer["total_currency"]).upper(),
            },
            offer_expires_at=cls._utc_timestamp(offer["expires_at"]),
            owner_name=str(owner.get("name") or "Unknown airline"),
            owner_iata_code=(
                str(owner["iata_code"]).upper() if owner.get("iata_code") else None
            ),
            passenger_count=passenger_count,
            live_mode=live_mode,
            availability_status="live_offer" if live_mode else "provider_test_offer",
        )

    def search(self, command: FlightSearchCommand) -> ProviderSearchBatch:
        if not self._token:
            raise DuffelFlightSearchProviderError(
                "DUFFEL_TOKEN is required for the Duffel provider"
            )
        request_body = {
            "data": {
                "slices": [
                    {
                        "origin": command.origin,
                        "destination": command.destination,
                        "departure_date": command.departure_date,
                    }
                ],
                "passengers": [
                    {"type": "adult"} for _ in range(command.passenger_count)
                ],
                "cabin_class": command.cabin_class,
            }
        }
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.post(
                    f"{self._base_url}/air/offer_requests",
                    params={
                        "return_offers": "true",
                        "supplier_timeout": str(self._supplier_timeout_ms),
                    },
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Duffel-Version": "v2",
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                        "Content-Type": "application/json",
                    },
                    json=request_body,
                )
        except httpx.HTTPError:
            raise DuffelFlightSearchProviderError("Duffel request failed") from None

        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        if response.is_error:
            code = "provider_error"
            if isinstance(payload, dict) and isinstance(payload.get("errors"), list):
                first = payload["errors"][0] if payload["errors"] else {}
                if isinstance(first, dict):
                    code = self._safe_code(first.get("code"))
            raise DuffelFlightSearchProviderError(
                f"Duffel HTTP {response.status_code}: {code}"
            ) from None
        data = payload.get("data") if isinstance(payload, dict) else None
        raw_offers = data.get("offers") if isinstance(data, dict) else None
        if not isinstance(raw_offers, list):
            raise DuffelFlightSearchProviderError("Duffel returned invalid offers")

        options: list[ProviderFlightOption] = []
        for raw_offer in raw_offers:
            if not isinstance(raw_offer, dict):
                continue
            try:
                options.append(
                    self._normalize_offer(
                        raw_offer, passenger_count=command.passenger_count
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        options.sort(
            key=lambda option: (
                parse_timestamp(option.segments[-1].arrival_at),
                Decimal(option.price.amount) if option.price else Decimal("Infinity"),
                option.option_id,
            )
        )
        options = options[: self._maximum_offers]
        if raw_offers and not options:
            raise DuffelFlightSearchProviderError(
                "Duffel offers could not be normalized"
            )

        response_live = bool(data.get("live_mode")) if isinstance(data, dict) else False
        live_mode = bool(options) and response_live and all(
            option.live_mode is True for option in options
        )
        return ProviderSearchBatch(
            source_scope="live_offers" if live_mode else "provider_test_offers",
            options=options,
            availability_verified=live_mode,
        )


def run_provider_search(
    command: FlightSearchCommand,
    provider: FlightSearchProvider,
) -> FlightSearchToolResult:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    batch = provider.search(command)
    return FlightSearchToolResult(
        search_id=command.search_id,
        decision_id=command.approval.decision_id,
        idempotency_key=command.idempotency_key,
        provider=provider.name,
        source_scope=batch.source_scope,
        searched_at=now,
        options=batch.options,
        availability_verified=batch.availability_verified,
    )


def rank_feasible_options(
    command: FlightSearchCommand,
    result: FlightSearchToolResult,
) -> tuple[list[RankedFlightOption], dict[str, int]]:
    """Apply deterministic safety/feasibility gates, then rank by arrival."""
    earliest = parse_timestamp(command.earliest_departure_at)
    latest = parse_timestamp(command.latest_departure_at)
    rejected: Counter[str] = Counter()
    eligible: list[ProviderFlightOption] = []

    for option in result.options:
        segments = option.segments
        departure = parse_timestamp(segments[0].departure_at)
        arrival = parse_timestamp(segments[-1].arrival_at)
        reason = None
        if segments[0].origin != command.origin or segments[-1].destination != command.destination:
            reason = "ROUTE_MISMATCH"
        elif departure < earliest or departure > latest:
            reason = "OUTSIDE_SEARCH_WINDOW"
        elif len(segments) - 1 > command.maximum_stops:
            reason = "TOO_MANY_STOPS"
        elif option.offer_expires_at is not None and parse_timestamp(
            option.offer_expires_at
        ) <= parse_timestamp(result.searched_at):
            reason = "OFFER_EXPIRED"
        elif any(
            parse_timestamp(segment.arrival_at)
            <= parse_timestamp(segment.departure_at)
            for segment in segments
        ):
            reason = "INVALID_SEGMENT_TIME"
        elif any(
            segments[index].destination != segments[index + 1].origin
            for index in range(len(segments) - 1)
        ):
            reason = "BROKEN_CONNECTION"
        elif any(
            (
                parse_timestamp(segments[index + 1].departure_at)
                - parse_timestamp(segments[index].arrival_at)
            ).total_seconds()
            < command.minimum_connection_minutes * 60
            for index in range(len(segments) - 1)
        ):
            reason = "CONNECTION_TOO_SHORT"
        elif arrival <= departure:
            reason = "INVALID_ITINERARY_TIME"
        elif any(
            segment.flight_iata == command.original_flight_iata
            for segment in segments
        ):
            reason = "ORIGINAL_FLIGHT"

        if reason is not None:
            rejected[reason] += 1
        else:
            eligible.append(option)

    eligible.sort(
        key=lambda option: (
            parse_timestamp(option.segments[-1].arrival_at),
            len(option.segments) - 1,
            Decimal(option.price.amount) if option.price else Decimal("Infinity"),
            parse_timestamp(option.segments[0].departure_at),
            option.option_id,
        )
    )
    ranked = [
        RankedFlightOption(
            rank=index,
            option_id=option.option_id,
            segments=option.segments,
            stops=len(option.segments) - 1,
            departure_at=option.segments[0].departure_at,
            arrival_at=option.segments[-1].arrival_at,
            price=option.price,
            offer_expires_at=option.offer_expires_at,
            owner_name=option.owner_name,
            owner_iata_code=option.owner_iata_code,
            passenger_count=option.passenger_count,
            live_mode=option.live_mode,
            availability_status=option.availability_status,
        )
        for index, option in enumerate(eligible, start=1)
    ]
    return ranked, dict(sorted(rejected.items()))
