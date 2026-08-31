from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from flight_ui.api_client import TravelApiClient, TravelApiError
from flight_ui.presentation import (
    active_leg_count,
    format_instant,
    make_upload_identity,
    mask_confirmation,
    normalize_phone_number,
    notification_feedback,
    next_poll_at,
    safe_pdf_filename,
    trip_status,
    validate_pdf,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("status,severity", [
    ("failed", "error"), ("rejected", "error"), ("accepted", "warning"),
    ("delivered", "success"), ("pending", "info"), ("duplicate", "info"),
])
def test_notification_feedback_does_not_confuse_approval_with_delivery(status, severity):
    level, message = notification_feedback({"notification_status": status})
    assert level == severity
    if status == "accepted":
        assert "has not been confirmed" in message


def test_trial_failure_explains_upgrade_instead_of_showing_success():
    level, message = notification_feedback({
        "notification_status": "failed", "notification_error_code": "TWILIO_HTTP_400",
        "notification_remediation": "upgrade_or_use_trial_template",
    })
    assert level == "error"
    assert "TWILIO_HTTP_400" in message
    assert "Upgrade" in message


def test_streamlit_demo_renders_failed_sms_as_error(monkeypatch):
    streamlit = pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    import flight_ui.api_client as api_client_module

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def health(self):
            return True

    monkeypatch.setattr(api_client_module, "TravelApiClient", FakeClient)
    monkeypatch.setenv("FLIGHT_AGENCY_DEMO_ENABLED", "true")
    streamlit.cache_resource.clear()
    app = AppTest.from_file(ROOT / "streamlit_app.py", default_timeout=8)
    app.session_state["active_trip"] = activation_payload()
    app.session_state["active_trip_id"] = "trip-ui-golden"
    app.session_state["agency_demo_trip_id"] = "trip-ui-golden"
    app.session_state["agency_demo_flights"] = [{
        "flight_iata": "SB410", "origin": "MAN", "destination": "FRA",
        "flight_date": "2026-09-20", "status": "scheduled",
    }]
    app.session_state["agency_demo_last_check"] = {"results": [{
        "monitoring_status": "candidate_evaluated", "category": "DELAY", "verdict": "NOTIFY",
        "notification_status": "failed", "notification_error_code": "TWILIO_HTTP_400",
        "notification_remediation": "upgrade_or_use_trial_template",
        "notification_message": "Your flight is delayed by 45 minutes.",
    }]}
    app.run()
    assert not app.exception
    assert any("TWILIO_HTTP_400" in error.value and "Upgrade" in error.value for error in app.error)
    assert any("not proof of SMS delivery" in caption.value for caption in app.caption)


def activation_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "status": "activated",
        "trip_id": "trip-ui-golden",
        "trip_status": "active",
        "parse_status": "parsed",
        "document": {
            "bucket": "travel-itineraries",
            "key": "opaque-document-key",
            "sha256": "a" * 64,
        },
        "itinerary": {
            "schema_version": "1.0.0",
            "trip_id": "trip-ui-golden",
            "traveler_ref": "traveler-ui-golden",
            "confirmation_codes": ["SB8M2P"],
            "legs": [
                {
                    "leg_id": "leg-ui-golden-1",
                    "marketing_carrier": "SB",
                    "operating_carrier": "SB",
                    "flight_number": "SB410",
                    "origin": "MAN",
                    "destination": "FRA",
                    "scheduled_departure_at": "2026-09-20T07:10:00Z",
                    "scheduled_arrival_at": "2026-09-20T08:55:00Z",
                }
            ],
        },
        "active_leg_count": 1,
        "next_poll_at": "2026-09-20T06:10:00Z",
        "idempotent_replay": False,
    }


def test_pdf_validation_and_upload_identity_are_safe_and_stable() -> None:
    content = b"%PDF-1.7\nsynthetic"
    trip_id, fixture_id, digest = make_upload_identity(
        content, token_factory=lambda _: "abc123"
    )

    assert validate_pdf(content) is None
    assert validate_pdf(b"not-a-pdf") == "This does not look like a valid PDF ticket."
    assert trip_id == f"trip-{digest[:12]}-abc123"
    assert fixture_id == f"upload-{digest[:12]}-abc123"
    assert safe_pdf_filename("../boarding-pass.PDF") == "boarding-pass.PDF"
    assert safe_pdf_filename("..\\boarding-pass.PDF") == "boarding-pass.PDF"
    assert normalize_phone_number("+44 7700 900123") == "+447700900123"
    assert normalize_phone_number("0044 (7700) 900-123") == "+447700900123"


def test_trip_presentation_hides_booking_reference_and_summarizes_state() -> None:
    payload = activation_payload()

    assert mask_confirmation("SB8M2P") == "••••2P"
    assert format_instant("2026-09-20T07:10:00Z") == "Sun 20 Sep · 07:10 UTC"
    assert trip_status(payload) == "active"
    assert active_leg_count(payload) == 1
    assert next_poll_at(payload) == "2026-09-20T06:10:00Z"


def test_api_client_uploads_pdf_to_trip_activation_boundary() -> None:
    observed: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["method"] = request.method
        observed["path"] = request.url.path
        observed["content"] = request.content
        return httpx.Response(200, request=request, json=activation_payload())

    http_client = httpx.Client(
        base_url="http://travel-api.test",
        transport=httpx.MockTransport(handler),
    )
    client = TravelApiClient("http://travel-api.test", client=http_client)

    result = client.activate_trip(
        document_bytes=b"%PDF-1.7\nsynthetic",
        filename="ticket.pdf",
        trip_id="trip-ui-golden",
        traveler_ref="traveler-ui-golden",
        fixture_id="upload-ui-golden",
        phone_e164="+447700900123",
        sms_consent=True,
    )

    assert result["status"] == "activated"
    assert observed["method"] == "POST"
    assert observed["path"] == "/v1/trips/activate"
    assert b'trip-ui-golden' in observed["content"]
    assert b'filename="ticket.pdf"' in observed["content"]
    assert b'+447700900123' in observed["content"]


def test_api_client_does_not_leak_upstream_error_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            request=request,
            json={"detail": "MISTRAL_API_KEY=should-never-be-shown"},
        )

    http_client = httpx.Client(
        base_url="http://travel-api.test",
        transport=httpx.MockTransport(handler),
    )
    client = TravelApiClient("http://travel-api.test", client=http_client)

    with pytest.raises(TravelApiError) as captured:
        client.get_trip("trip-ui-golden")

    assert captured.value.retryable is True
    assert "temporarily unavailable" in str(captured.value)
    assert "MISTRAL" not in str(captured.value)


def test_streamlit_customer_can_upload_and_see_expected_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    streamlit = pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    import flight_ui.api_client as api_client_module

    captured_activation: dict[str, Any] = {}

    class FakeTravelApiClient:
        def __init__(self, base_url: str, *, timeout_seconds: float) -> None:
            self.base_url = base_url
            self.timeout_seconds = timeout_seconds

        def health(self) -> bool:
            return True

        def activate_trip(self, **kwargs: Any) -> dict[str, Any]:
            captured_activation.update(kwargs)
            return activation_payload()

        def get_trip(self, trip_id: str) -> dict[str, Any]:
            assert trip_id.startswith("trip-")
            return activation_payload()

    monkeypatch.setattr(api_client_module, "TravelApiClient", FakeTravelApiClient)
    streamlit.cache_resource.clear()
    app = AppTest.from_file(ROOT / "streamlit_app.py", default_timeout=8).run()

    assert not app.exception
    assert any(title.value == "Your ticket in. Travel stress out." for title in app.title)
    assert len(app.file_uploader) == 1
    assert app.checkbox[0].value is False
    assert app.checkbox[1].value is False

    app.text_input[0].input("Sam")
    app.text_input[1].input("+44 7700 900123")
    app.file_uploader[0].upload(
        "ticket.pdf",
        b"%PDF-1.7\nsynthetic customer ticket",
        "application/pdf",
    )
    app.checkbox[0].check()
    app.checkbox[1].check()
    submit = next(button for button in app.button if button.label == "Start watching my trip")
    submit.click()
    app.run(timeout=8)

    assert not app.exception
    assert any(title.value == "You’re all set, Sam" for title in app.title)
    assert any(metric.label == "Flights being watched" and metric.value == "1" for metric in app.metric)
    assert any("MAN → FRA" in markdown.value for markdown in app.markdown)
    assert all("SB8M2P" not in caption.value for caption in app.caption)
    assert captured_activation["phone_e164"] == "+447700900123"
    assert captured_activation["sms_consent"] is True
