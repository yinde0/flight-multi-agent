from __future__ import annotations

import re

from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Literal
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
IATA_IN_PARENS_RE = re.compile(r"\(([A-Z]{3})\)")
TRAVEL_DATE_RE = re.compile(
    r"\b(?P<day>[0-3]?[0-9])\s+(?P<month>[A-Z]{3})\s+(?P<year>20[0-9]{2})\b",
    re.IGNORECASE,
)
AMBIGUOUS_FLIGHT_RE = re.compile(r"\b[A-Z0-9]{2,3}\s+[0-9?]*\?[0-9?]*\b")
AMBIGUOUS_DEPARTURE_RE = re.compile(
    r"\bdeparture\s+[0-2]?[0-9]:[0-5?][0-9?]", re.IGNORECASE
)


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
    metadata: DocumentMetadata,
    *,
    reason_codes: list[str],
    safe_partial_extraction: dict[str, Any] | None = None,
    must_not_infer: list[str] | None = None,
    orchestration: dict[str, Any] | None = None,
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
            "safe_partial_extraction": safe_partial_extraction or {},
            "must_not_infer": must_not_infer
            or [
                "confirmation_code",
                "flight_number",
                "scheduled_departure_at",
            ],
        },
        orchestration=orchestration
        or _orchestration("pdf_text_layer", "request_human_review"),
    )


def _orchestration(
    text_source: Literal["pdf_text_layer", "mistral_ocr"],
    final_step: Literal["structure_itinerary", "request_human_review"],
    *,
    ocr_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    steps = ["extract_pdf_text"]
    if text_source == "mistral_ocr":
        steps.append("mistral_ocr")
    steps.append(final_step)
    result: dict[str, Any] = {
        "framework": "crewai-flow",
        "steps": steps,
        "llm_calls": 0,
        "ocr_calls": 1 if text_source == "mistral_ocr" else 0,
        "text_source": text_source,
    }
    if ocr_details:
        result["ocr"] = ocr_details
    return result


def _safe_partial_from_ocr(text: str) -> dict[str, str]:
    partial: dict[str, str] = {}
    airports: list[str] = []
    for airport in IATA_IN_PARENS_RE.findall(text.upper()):
        if airport not in airports:
            airports.append(airport)
    if len(airports) >= 2:
        partial["origin"] = airports[0]
        partial["destination"] = airports[1]

    match = TRAVEL_DATE_RE.search(text)
    if match:
        try:
            parsed = datetime.strptime(
                " ".join(
                    [match.group("day"), match.group("month"), match.group("year")]
                ),
                "%d %b %Y",
            )
            partial["travel_date"] = parsed.date().isoformat()
        except ValueError:
            pass
    return partial


def _ocr_review_reasons(text: str) -> list[str]:
    reasons: list[str] = []
    normalized_lines = [line.strip("#* _`") for line in text.splitlines()]
    booking_label_indexes = [
        index
        for index, line in enumerate(normalized_lines)
        if line.lower() == "booking reference"
    ]
    has_confirmation_code = False
    for index in booking_label_indexes:
        following_line = next(
            (line for line in normalized_lines[index + 1 :] if line), ""
        )
        has_confirmation_code = bool(PNR_RE.fullmatch(following_line))
        if has_confirmation_code:
            break
    if booking_label_indexes and not has_confirmation_code:
        reasons.append("CONFIRMATION_CODE_REDACTED")
    if AMBIGUOUS_FLIGHT_RE.search(text.upper()):
        reasons.append("LOW_OCR_CONFIDENCE_FLIGHT_NUMBER")
    if "?" in text and AMBIGUOUS_DEPARTURE_RE.search(text):
        reasons.append("LOW_OCR_CONFIDENCE_DEPARTURE_TIME")
    return reasons or ["OCR_TEXT_INCOMPLETE"]


def parse_extracted_text(
    text: str,
    metadata: DocumentMetadata,
    *,
    text_source: Literal["pdf_text_layer", "mistral_ocr"] = "pdf_text_layer",
    ocr_details: dict[str, Any] | None = None,
) -> ParseOutcome:
    if not text.strip():
        return review_outcome(
            metadata,
            reason_codes=[
                "CONFIRMATION_CODE_REDACTED",
                "LOW_OCR_CONFIDENCE_FLIGHT_NUMBER",
                "LOW_OCR_CONFIDENCE_DEPARTURE_TIME",
            ],
            orchestration=_orchestration(
                text_source, "request_human_review", ocr_details=ocr_details
            ),
        )

    try:
        itinerary = structure_itinerary(text, metadata)
    except (IncompleteItineraryError, ValueError):
        if text_source == "mistral_ocr":
            return review_outcome(
                metadata,
                reason_codes=_ocr_review_reasons(text),
                safe_partial_extraction=_safe_partial_from_ocr(text),
                orchestration=_orchestration(
                    text_source, "request_human_review", ocr_details=ocr_details
                ),
            )
        return review_outcome(
            metadata,
            reason_codes=["MACHINE_READABLE_TEXT_INCOMPLETE"],
            orchestration=_orchestration(
                text_source, "request_human_review", ocr_details=ocr_details
            ),
        )

    return ParseOutcome(
        status="parsed",
        document=metadata,
        itinerary=itinerary,
        orchestration=_orchestration(
            text_source, "structure_itinerary", ocr_details=ocr_details
        ),
    )
