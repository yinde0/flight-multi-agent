from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


MAX_PDF_BYTES = 5 * 1024 * 1024


def validate_pdf(document_bytes: bytes) -> str | None:
    if not document_bytes:
        return "Choose a PDF ticket to continue."
    if len(document_bytes) > MAX_PDF_BYTES:
        return "That PDF is larger than 5 MB. Please upload a smaller copy."
    if not document_bytes.startswith(b"%PDF-"):
        return "This does not look like a valid PDF ticket."
    return None


def make_upload_identity(
    document_bytes: bytes,
    token_factory: Callable[[int], str] = secrets.token_hex,
) -> tuple[str, str, str]:
    digest = hashlib.sha256(document_bytes).hexdigest()
    suffix = token_factory(16).lower()
    return (
        f"trip-{digest[:12]}-{suffix}",
        f"upload-{digest[:12]}-{suffix}",
        digest,
    )


def safe_pdf_filename(filename: str | None) -> str:
    candidate = Path((filename or "itinerary.pdf").replace("\\", "/")).name
    if not candidate.lower().endswith(".pdf"):
        return "itinerary.pdf"
    return candidate[:255]


def mask_confirmation(code: str) -> str:
    cleaned = code.strip()
    if len(cleaned) <= 2:
        return "•" * len(cleaned)
    return f"{'•' * (len(cleaned) - 2)}{cleaned[-2:]}"


def format_instant(value: str | None) -> str:
    if not value:
        return "Not scheduled"
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "Schedule unavailable"
    zone = instant.tzname() or "local"
    return f"{instant:%a %d %b · %H:%M} {zone}"


def trip_status(payload: dict[str, Any]) -> str:
    status = str(payload.get("trip_status") or payload.get("status") or "unknown")
    if status in {"activated", "already_active"}:
        return "active"
    return status


def active_leg_count(payload: dict[str, Any]) -> int:
    if isinstance(payload.get("active_leg_count"), int):
        return int(payload["active_leg_count"])
    legs = payload.get("legs")
    if not isinstance(legs, list):
        itinerary = payload.get("itinerary")
        legs = itinerary.get("legs", []) if isinstance(itinerary, dict) else []
        return len(legs)
    return sum(
        1
        for leg in legs
        if isinstance(leg, dict) and leg.get("monitoring_status") == "active"
    )


def next_poll_at(payload: dict[str, Any]) -> str | None:
    direct = payload.get("next_poll_at")
    if isinstance(direct, str):
        return direct
    legs = payload.get("legs")
    if not isinstance(legs, list):
        return None
    candidates = sorted(
        str(leg["next_poll_at"])
        for leg in legs
        if isinstance(leg, dict) and leg.get("next_poll_at")
    )
    return candidates[0] if candidates else None
