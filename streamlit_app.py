from __future__ import annotations

import os
import secrets
from typing import Any

import streamlit as st
from streamlit.typing import UploadedFile

from flight_ui.api_client import TravelApiClient, TravelApiError
from flight_ui.presentation import (
    active_leg_count,
    format_instant,
    make_upload_identity,
    mask_confirmation,
    next_poll_at,
    normalize_phone_number,
    safe_pdf_filename,
    trip_status,
    validate_pdf,
)


st.set_page_config(
    page_title="Travel Watch",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _initialize_session() -> None:
    defaults: dict[str, Any] = {
        "active_trip": None,
        "active_trip_id": None,
        "customer_name": "",
        "traveler_ref": f"traveler-{secrets.token_hex(8)}",
        "pending_upload_sha": None,
        "pending_trip_id": None,
        "pending_fixture_id": None,
        "sms_enabled": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


@st.cache_resource
def _api_client(base_url: str, timeout_seconds: float) -> TravelApiClient:
    return TravelApiClient(base_url, timeout_seconds=timeout_seconds)


def _clear_trip() -> None:
    st.session_state.active_trip = None
    st.session_state.active_trip_id = None
    st.session_state.customer_name = ""
    st.session_state.pending_upload_sha = None
    st.session_state.pending_trip_id = None
    st.session_state.pending_fixture_id = None
    st.session_state.sms_enabled = False
    st.session_state.traveler_ref = f"traveler-{secrets.token_hex(8)}"


def _identity_for(document_bytes: bytes) -> tuple[str, str]:
    trip_id, fixture_id, digest = make_upload_identity(document_bytes)
    if st.session_state.pending_upload_sha != digest:
        st.session_state.pending_upload_sha = digest
        st.session_state.pending_trip_id = trip_id
        st.session_state.pending_fixture_id = fixture_id
    return (
        str(st.session_state.pending_trip_id),
        str(st.session_state.pending_fixture_id),
    )


def _render_sidebar(client: TravelApiClient) -> None:
    with st.sidebar:
        st.title("Travel Watch")
        if client.health():
            st.success("Travel services connected", icon=":material/cloud_done:")
        else:
            st.warning("Travel services are waking up", icon=":material/cloud_off:")

        st.subheader("What we do")
        st.markdown(
            "1. Read your ticket\n"
            "2. Watch flight and weather changes\n"
            "3. Alert you only when it matters"
        )

        if st.session_state.active_trip_id:
            st.subheader("Your trip reference")
            st.code(st.session_state.active_trip_id, language=None)
            st.caption("Keep this private. Account-based trip recovery comes next.")
            if st.session_state.sms_enabled:
                st.success("Important SMS alerts enabled", icon=":material/sms:")

        st.divider()
        st.caption(
            "We never book a replacement flight automatically. Rebooking searches are "
            "read-only until you choose what to do."
        )


def _render_hero() -> None:
    st.title("Your ticket in. Travel stress out.")
    st.markdown(
        "### Upload your flight PDF once. We’ll read the itinerary, watch for meaningful "
        "disruptions, and keep quiet about minor noise."
    )
    with st.container(horizontal=True, wrap=True, gap="small"):
        st.badge("PDF and scanned tickets", icon=":material/picture_as_pdf:", color="blue")
        st.badge("Flight + weather watch", icon=":material/radar:", color="green")
        st.badge("No alert spam", icon=":material/notifications_active:", color="violet")


def _render_upload(client: TravelApiClient) -> None:
    st.subheader("Add your trip", anchor=False)
    st.caption("PDF only · up to 5 MB · one itinerary at a time")

    with st.form("ticket_upload_form", border=True):
        name = st.text_input(
            "What should we call you?",
            placeholder="Sam",
            max_chars=80,
            help="Used only to personalise this browser session.",
        )
        ticket: UploadedFile | None = st.file_uploader(
            "Upload your e-ticket or itinerary",
            type="pdf",
            max_upload_size=5,
            help="Text PDFs and scanned image-only PDFs are supported.",
        )
        st.markdown("**Important disruption alerts**")
        phone_number = st.text_input(
            "Mobile number",
            placeholder="+44 7700 900123",
            help="Include your country code. We do not display this number in your trip view.",
            max_chars=30,
        )
        sms_consent = st.checkbox(
            "Text me only when a disruption is significant. Message and data rates may apply. Reply STOP to opt out.",
            value=False,
        )
        consent = st.checkbox(
            "I want Travel Watch to store this ticket and monitor its flights."
        )
        submitted = st.form_submit_button(
            "Start watching my trip",
            type="primary",
            icon=":material/add_alert:",
            width="stretch",
        )

    if not submitted:
        return
    clean_name = name.strip()
    if len(clean_name) < 2:
        st.error("Tell us what to call you before continuing.", icon=":material/person:")
        return
    if ticket is None:
        st.error("Choose your PDF ticket before continuing.", icon=":material/upload_file:")
        return
    if not consent:
        st.error("Please confirm that we may store and monitor this trip.", icon=":material/shield:")
        return
    normalized_phone = None
    if sms_consent:
        try:
            normalized_phone = normalize_phone_number(phone_number)
        except ValueError as error:
            st.error(str(error), icon=":material/smartphone:")
            return
    elif phone_number.strip():
        st.error(
            "Clear the mobile number or consent to important SMS alerts.",
            icon=":material/sms_failed:",
        )
        return

    document_bytes = ticket.getvalue()
    validation_error = validate_pdf(document_bytes)
    if validation_error:
        st.error(validation_error, icon=":material/picture_as_pdf:")
        return

    trip_id, fixture_id = _identity_for(document_bytes)
    with st.status("Reading your ticket…", expanded=True) as status:
        st.write("Uploading the PDF securely")
        try:
            outcome = client.activate_trip(
                document_bytes=document_bytes,
                filename=safe_pdf_filename(ticket.name),
                trip_id=trip_id,
                traveler_ref=str(st.session_state.traveler_ref),
                fixture_id=fixture_id,
                phone_e164=normalized_phone,
                sms_consent=sms_consent,
            )
        except TravelApiError as error:
            status.update(label="We could not add this trip", state="error", expanded=True)
            st.error(str(error), icon=":material/error:")
            return
        st.write("Turning each flight into a monitoring watch")
        status.update(label="Your trip is ready", state="complete", expanded=False)

    st.session_state.customer_name = clean_name
    st.session_state.active_trip_id = trip_id
    st.session_state.active_trip = outcome
    st.session_state.sms_enabled = sms_consent
    st.toast("Your flights are now being watched", icon=":material/check_circle:")


def _render_review(payload: dict[str, Any]) -> None:
    st.warning(
        "We stored the ticket safely, but some flight details need a human check before "
        "monitoring can begin.",
        icon=":material/rule:",
    )
    review = payload.get("review")
    if isinstance(review, dict):
        missing = review.get("missing_fields")
        if isinstance(missing, list) and missing:
            st.caption("Missing details: " + ", ".join(str(item) for item in missing[:6]))
    st.info(
        "Automatic correction is deliberately disabled so we do not watch the wrong flight."
    )


def _render_itinerary(payload: dict[str, Any]) -> None:
    itinerary = payload.get("itinerary")
    if not isinstance(itinerary, dict):
        return
    legs = itinerary.get("legs")
    if not isinstance(legs, list):
        return

    st.subheader("Your journey", anchor=False)
    confirmation_codes = itinerary.get("confirmation_codes")
    if isinstance(confirmation_codes, list) and confirmation_codes:
        masked = ", ".join(mask_confirmation(str(code)) for code in confirmation_codes)
        st.caption(f"Booking reference: {masked}")

    for index, leg in enumerate(legs, start=1):
        if not isinstance(leg, dict):
            continue
        origin = str(leg.get("origin") or "—")
        destination = str(leg.get("destination") or "—")
        flight_number = str(leg.get("flight_number") or "Flight pending")
        with st.container(border=True):
            with st.container(
                horizontal=True,
                wrap=True,
                horizontal_alignment="distribute",
                vertical_alignment="center",
            ):
                st.markdown(f"#### {origin} → {destination}")
                st.badge(f"Leg {index}", color="gray")
                st.badge(flight_number, icon=":material/flight:", color="blue")
            departure, arrival = st.columns(2)
            departure.metric(
                "Departure",
                format_instant(leg.get("scheduled_departure_at")),
                border=True,
            )
            arrival.metric(
                "Arrival",
                format_instant(leg.get("scheduled_arrival_at")),
                border=True,
            )


def _render_trip(client: TravelApiClient) -> None:
    payload = st.session_state.active_trip
    if not isinstance(payload, dict):
        return

    name = st.session_state.customer_name or "traveler"
    status_value = trip_status(payload)
    if status_value == "review_required":
        st.title(f"We need a quick check, {name}")
    else:
        st.title(f"You’re all set, {name}")
        st.success(
            "Your ticket is stored and meaningful flight disruptions will be monitored. "
            + (
                "Important alerts will be sent by SMS."
                if st.session_state.sms_enabled
                else ""
            ),
            icon=":material/travel_explore:",
        )

    actions = st.container(horizontal=True, wrap=True, gap="small")
    if actions.button(
        "Refresh trip",
        type="primary",
        icon=":material/refresh:",
    ):
        try:
            st.session_state.active_trip = client.get_trip(
                str(st.session_state.active_trip_id)
            )
            st.toast("Trip status refreshed", icon=":material/sync:")
            st.rerun()
        except TravelApiError as error:
            st.error(str(error), icon=":material/cloud_off:")
    if actions.button("Add another ticket", icon=":material/add:"):
        _clear_trip()
        st.rerun()

    active_count = active_leg_count(payload)
    next_check = format_instant(next_poll_at(payload))
    metric_columns = st.columns(3)
    metric_columns[0].metric("Trip status", status_value.replace("_", " ").title(), border=True)
    metric_columns[1].metric("Flights being watched", active_count, border=True)
    metric_columns[2].metric("Next automatic check", next_check, border=True)

    if status_value == "review_required":
        _render_review(payload)
    _render_itinerary(payload)

    stored_legs = payload.get("legs")
    if isinstance(stored_legs, list) and stored_legs:
        with st.expander("Monitoring activity"):
            for leg in stored_legs:
                if not isinstance(leg, dict):
                    continue
                flight = str(leg.get("flight_iata") or "Flight")
                route = f"{leg.get('origin', '—')} → {leg.get('destination', '—')}"
                poll_count = int(leg.get("poll_count") or 0)
                st.markdown(f"**{flight} · {route}**")
                st.caption(f"Checks completed: {poll_count} · Last result: {leg.get('last_poll_status') or 'Waiting'}")

    with st.expander("How we decide when to alert you"):
        st.markdown(
            "- Gate-only changes stay quiet.\n"
            "- Delays under 30 minutes are suppressed.\n"
            "- Repeated unchanged updates are ignored.\n"
            "- Cancellations and serious connection risks can trigger an alert and a read-only flight search."
        )


_initialize_session()
api_url = os.getenv("TRAVEL_API_URL", "http://127.0.0.1:8080")
api_timeout = float(os.getenv("TRAVEL_UI_API_TIMEOUT_SECONDS", "150"))
api_client = _api_client(api_url, api_timeout)

_render_sidebar(api_client)
if st.session_state.active_trip is None:
    _render_hero()
    _render_upload(api_client)
if st.session_state.active_trip is not None:
    _render_trip(api_client)

st.divider()
st.caption("Travel Watch · Calm monitoring for the journey ahead")
