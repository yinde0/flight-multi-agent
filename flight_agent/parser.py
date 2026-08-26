from __future__ import annotations

import re

from datetime import datetime, timezone
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from flight_agent.contracts import DocumentMetadata, ParseOutcome


AIRPORT_TIMEZONES = {
    "AMS": "Europe/Amsterdam",
    "ATH": "Europe/Athens",
    "FRA": "Europe/Berlin",
    "LHR": "Europe/London",
    "MAN": "Europe/London",
}
ROUTE_RE = re.compile(r"^(?P<origin>[A-Z]{3})\s+to\s+(?P<destination>[A-Z]{3})$")
FLIGHT_RE = re.compile(r"^(?P<carrier>[A-Z0-9]{2,3})\s+(?P<number>[0-9]{1,4})$")
PNR_RE = re.compile(r"^[A-Z0-9]{6}$")


class IncompleteItineraryError(ValueError):
    """The text exists, but required itinerary fields cannot be extracted safely."""


def extract_pdf_text(document_bytes: bytes) -> str:
    """Extract only the PDF text layer; no OCR or guessing is performed."""

    reader = PdfReader(BytesIO(document_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages).strip()


def _utc_timestamp(date_text: str, time_text: str, airport: str) -> str:
    timezone_name = AIRPORT_TIMEZONES.get(airport)
    if not timezone_name:
        raise IncompleteItineraryError(f"No timezone mapping for airport {airport}")
    local = datetime.strptime(
        f"{date_text} {time_text}", "%d %b %Y %H:%M"
    ).replace(tzinfo=ZoneInfo(timezone_name))
    return local.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _confirmation_code(lines: list[str]) -> str:
    for index, line in enumerate(lines[:-1]):
        if line.lower() == "booking reference" and PNR_RE.fullmatch(lines[index + 1]):
            return lines[index + 1]
    raise IncompleteItineraryError("Booking reference is missing or ambiguous")


def _leg_id(trip_id: str, sequence: int) -> str:
    return f"leg-{trip_id.removeprefix('trip-')}-{sequence}"


def structure_itinerary(text: str, metadata: DocumentMetadata) -> dict[str, Any]:
    """Parse the synthetic airline layout into the canonical itinerary contract."""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    confirmation_code = _confirmation_code(lines)
    legs: list[dict[str, Any]] = []

    for index, line in enumerate(lines):
        route = ROUTE_RE.fullmatch(line)
        if not route:
            continue
        if index + 11 >= len(lines):
            raise IncompleteItineraryError("Flight block is truncated")

        flight = FLIGHT_RE.fullmatch(lines[index + 1])
        if not flight:
            raise IncompleteItineraryError("Flight number is missing or ambiguous")

        date_text = lines[index + 3]
        departure_time = lines[index + 8]
        arrival_time = lines[index + 10]
        origin = route.group("origin")
        destination = route.group("destination")
        carrier = flight.group("carrier")

        leg: dict[str, Any] = {
            "leg_id": _leg_id(metadata.trip_id, len(legs) + 1),
            "marketing_carrier": carrier,
            "operating_carrier": carrier,
            "flight_number": f"{carrier}{flight.group('number')}",
            "origin": origin,
            "destination": destination,
            "scheduled_departure_at": _utc_timestamp(
                date_text, departure_time, origin
            ),
            "scheduled_arrival_at": _utc_timestamp(
                date_text, arrival_time, destination
            ),
        }
        legs.append(leg)

    if not legs:
        raise IncompleteItineraryError("No complete flight blocks were found")

    for leg in legs[:-1]:
        # First-slice domain rule. Replace with airport/carrier data in a later slice.
        leg["minimum_connection_minutes"] = 45

    return {
        "schema_version": "1.0.0",
        "trip_id": metadata.trip_id,
        "traveler_ref": metadata.traveler_ref,
        "confirmation_codes": [confirmation_code],
        "legs": legs,
    }


def review_outcome(
    metadata: DocumentMetadata, *, reason_codes: list[str]
) -> ParseOutcome:
    """Build an explicit abstention that contains no invented itinerary fields."""

    return ParseOutcome(
        status="review_required",
        document=metadata,
        review={
            "schema_version": "1.0.0",
            "fixture_id": metadata.fixture_id,
            "review_required": True,
            "reason_codes": reason_codes,
            "safe_partial_extraction": {},
            "must_not_infer": [
                "confirmation_code",
                "flight_number",
                "scheduled_departure_at",
            ],
        },
        orchestration={
            "framework": "crewai-flow",
            "steps": ["extract_pdf_text", "request_human_review"],
            "llm_calls": 0,
        },
    )


def parse_extracted_text(text: str, metadata: DocumentMetadata) -> ParseOutcome:
    if not text.strip():
        return review_outcome(
            metadata,
            reason_codes=[
                "CONFIRMATION_CODE_REDACTED",
                "LOW_OCR_CONFIDENCE_FLIGHT_NUMBER",
                "LOW_OCR_CONFIDENCE_DEPARTURE_TIME",
            ],
        )

    try:
        itinerary = structure_itinerary(text, metadata)
    except (IncompleteItineraryError, ValueError):
        return review_outcome(
            metadata,
            reason_codes=["MACHINE_READABLE_TEXT_INCOMPLETE"],
        )

    return ParseOutcome(
        status="parsed",
        document=metadata,
        itinerary=itinerary,
        orchestration={
            "framework": "crewai-flow",
            "steps": ["extract_pdf_text", "structure_itinerary"],
            "llm_calls": 0,
        },
    )
