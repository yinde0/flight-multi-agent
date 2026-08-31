from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def notification_feedback(result: dict) -> tuple[str, str]:
    """Eval approval, provider acceptance, and delivery are different outcomes."""

    status = str(result.get("notification_status") or "pending")
    if status in {"failed", "rejected"}:
        guidance = {
            "upgrade_or_use_trial_template": "Twilio Trial does not allow custom SMS text. Upgrade the account or explicitly use an approved trial template for testing.",
            "verify_recipient": "Verify the recipient in Twilio and check the account's trial restrictions.",
            "check_credentials": "Check the Twilio account credentials and permissions.",
            "check_sender": "Check that the sender is approved and SMS-capable for this account.",
            "check_delivery_before_retry": "Submission could not be confirmed. Check Twilio's message log before retrying to avoid a duplicate SMS.",
            "retry_later": "The provider is temporarily unavailable; the delivery worker will retry within its configured limit.",
        }.get(str(result.get("notification_remediation") or ""), "Check the notification error and provider configuration before retrying.")
        code = str(result.get("notification_error_code") or "UNKNOWN")
        return "error", f"Notification {status} ({code}). {guidance}"
    if status == "accepted":
        return "warning", "SMS submitted to the provider; delivery to your phone has not been confirmed."
    if status == "delivered":
        return "success", "Notification delivery confirmed by the configured provider."
    if status == "duplicate":
        return "info", "This alert was already submitted. No duplicate message was sent."
    return "info", "The alert is approved, but notification processing is still pending."

MAX_PDF_BYTES = 5 * 1024 * 1024
E164_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")


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


def normalize_phone_number(value: str) -> str:
    compact = re.sub(r"[\s().-]", "", value.strip())
    if compact.startswith("00"):
        compact = f"+{compact[2:]}"
    if E164_PATTERN.fullmatch(compact) is None:
        raise ValueError(
            "Enter a full international mobile number, for example +44 7700 900123."
        )
    return compact


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
