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
    notification_feedback,
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
        "agency_demo_enabled": os.getenv(
            "FLIGHT_AGENCY_DEMO_ENABLED", "false"
        ).lower()
        in {"1", "true", "yes"},
        "agency_demo_trip_id": None,
        "agency_demo_flights": [],
        "agency_demo_last_check": None,
        "agency_demo_pending_change": False,
        "agency_demo_error": None,
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
    st.session_state.agency_demo_trip_id = None
    st.session_state.agency_demo_flights = []
    st.session_state.agency_demo_last_check = None
    st.session_state.agency_demo_pending_change = False
    st.session_state.agency_demo_error = None
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

        if st.session_state.agency_demo_enabled:
            st.info(
                "Flight agency sandbox active",
                icon=":material/airline_seat_recline_normal:",
            )

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
        if st.session_state.agency_demo_enabled and outcome.get("itinerary"):
            st.write("Creating the matching flight in the demo control tower")
            try:
                synced = client.sync_agency_trip(trip_id)
                st.session_state.agency_demo_trip_id = trip_id
                st.session_state.agency_demo_flights = synced.get("flights", [])
                st.write("Storing the first on-time monitoring baseline")
                st.session_state.agency_demo_last_check = (
                    client.run_agency_demo_check(trip_id)
                )
                st.session_state.agency_demo_error = None
            except TravelApiError as error:
                st.session_state.agency_demo_error = str(error)
        status.update(label="Your trip is ready", state="complete", expanded=False)

    st.session_state.customer_name = clean_name
    st.session_state.active_trip_id = trip_id
    st.session_state.active_trip = outcome
    st.session_state.sms_enabled = sms_consent
    st.toast("Your flights are now being watched", icon=":material/check_circle:")


def _scenario_change(scenario: str, flight: dict[str, Any]) -> dict[str, Any] | None:
    if scenario == "On time":
        return None
    if scenario == "Gate change":
        current = str(flight.get("departure_gate") or "A10")
        return {
            "departure_gate": "C14" if current != "C14" else "A10",
            "note": "Operator moved the departure gate",
        }
    if scenario == "15 minute delay":
        return {
            "status": "scheduled",
            "departure_delay_minutes": 15,
            "arrival_delay_minutes": 15,
            "note": "Minor operational delay",
        }
    if scenario == "45 minute delay":
        return {
            "status": "scheduled",
            "departure_delay_minutes": 45,
            "arrival_delay_minutes": 45,
            "note": "Material operational delay",
        }
    if scenario == "90 minute delay":
        return {
            "status": "scheduled",
            "departure_delay_minutes": 90,
            "arrival_delay_minutes": 90,
            "note": "Severe operational delay",
        }
    if scenario == "Cancelled":
        return {"status": "cancelled", "note": "Flight cancelled by operator"}
    return {"status": "diverted", "note": "Flight diverted by operator"}


def _store_agency_flight(updated: dict[str, Any]) -> None:
    flights = st.session_state.agency_demo_flights
    st.session_state.agency_demo_flights = [
        updated
        if item.get("flight_iata") == updated.get("flight_iata")
        and item.get("flight_date") == updated.get("flight_date")
        else item
        for item in flights
        if isinstance(item, dict)
    ]


def _render_agency_check(payload: dict[str, Any] | None) -> None:
    if not isinstance(payload, dict):
        st.caption("The next monitoring result will appear here.")
        return
    results = payload.get("results")
    result = results[0] if isinstance(results, list) and results else {}
    monitoring_status = str(result.get("monitoring_status") or "No flight was due")
    category = str(result.get("category") or "No disruption")
    verdict = str(result.get("verdict") or "No decision required")
    with st.container(horizontal=True, wrap=True):
        st.metric("Monitor result", monitoring_status.replace("_", " "), border=True)
        st.metric("Candidate", category.replace("_", " "), border=True)
        st.metric("Evaluation", verdict.replace("_", " "), border=True)
    if verdict == "SUPPRESS":
        st.info(
            "The evaluator saw the change and deliberately kept the traveler quiet.",
            icon=":material/notifications_off:",
        )
    elif verdict in {"NOTIFY", "NOTIFY_AND_SEARCH"}:
        search = str(result.get("search_status") or "not required")
        severity, feedback = notification_feedback(result)
        if severity == "error":
            st.error(feedback, icon=":material/sms_failed:")
        elif severity == "warning":
            st.warning(feedback, icon=":material/pending:")
        elif severity == "success":
            st.success(feedback, icon=":material/notification_important:")
        else:
            st.info(feedback, icon=":material/notification_important:")
        st.caption(f"Rebooking search: {search}")
        message = str(result.get("notification_message") or "").strip()
        if message:
            st.caption("Prepared message — this preview is not proof of SMS delivery.")
            st.info(message, icon=":material/chat_bubble:")
    elif monitoring_status == "baseline_stored":
        st.success(
            "The on-time flight is now the comparison baseline.",
            icon=":material/database:",
        )


def _render_agency_demo(client: TravelApiClient) -> None:
    st.subheader("Flight agency control tower", anchor=False)
    st.caption(
        "Change the airline’s flight record, run one monitoring check, and watch the "
        "real evaluator decide whether the traveler should be disturbed."
    )

    trip_id = str(st.session_state.active_trip_id)
    if st.session_state.agency_demo_trip_id != trip_id:
        if st.button(
            "Connect this trip to the control tower",
            icon=":material/link:",
            type="primary",
        ):
            try:
                with st.status("Preparing the flight agency sandbox…") as status:
                    synced = client.sync_agency_trip(trip_id)
                    st.session_state.agency_demo_trip_id = trip_id
                    st.session_state.agency_demo_flights = synced.get("flights", [])
                    st.session_state.agency_demo_last_check = (
                        client.run_agency_demo_check(trip_id)
                    )
                    status.update(label="Control tower ready", state="complete")
                st.rerun()
            except TravelApiError as error:
                st.error(str(error), icon=":material/cloud_off:")
        return

    flights = [
        item
        for item in st.session_state.agency_demo_flights
        if isinstance(item, dict)
    ]
    if not flights:
        st.warning("No simulated flight is connected to this itinerary.")
        return

    flight_options = {
        f"{item.get('flight_iata')} · {item.get('origin')} → {item.get('destination')}": item
        for item in flights
    }
    selected_label = st.selectbox(
        "Flight to control",
        options=list(flight_options),
        key="agency_demo_selected_flight",
    )
    flight = flight_options[selected_label]
    status_value = str(flight.get("status") or "unknown")
    delay = int(flight.get("departure_delay_minutes") or 0)
    gate = str(flight.get("departure_gate") or "—")
    revision = int(flight.get("revision") or 1)
    with st.container(horizontal=True, wrap=True):
        st.metric("Airline status", status_value.title(), border=True)
        st.metric("Departure delay", f"{delay} min", border=True)
        st.metric("Departure gate", gate, border=True)
        st.metric("Agency revision", revision, border=True)

    scenario_help = {
        "On time": "Restore the original booked schedule.",
        "Gate change": "Expected: detected, but suppressed without an alert.",
        "15 minute delay": "Expected: below the 30-minute notification threshold.",
        "45 minute delay": "Expected: notify the traveler without searching.",
        "90 minute delay": "Expected: notify and search for alternatives.",
        "Cancelled": "Expected: notify and search for alternatives.",
        "Diverted": "Expected: notify and search for alternatives.",
    }
    with st.form("agency_scenario_form", border=True):
        scenario = st.selectbox("Choose an airline update", list(scenario_help))
        st.caption(scenario_help[scenario])
        apply_scenario = st.form_submit_button(
            "Apply flight update",
            icon=":material/edit_calendar:",
            type="primary",
        )

    if apply_scenario:
        try:
            if scenario == "On time":
                updated = client.reset_agency_flight(
                    str(flight["flight_iata"]), str(flight["flight_date"])
                )
            else:
                updated = client.change_agency_flight(
                    str(flight["flight_iata"]),
                    str(flight["flight_date"]),
                    _scenario_change(scenario, flight) or {},
                )
            _store_agency_flight(updated)
            st.session_state.agency_demo_pending_change = True
            st.toast(
                "Airline record changed. Travel Watch has not checked it yet.",
                icon=":material/flight_takeoff:",
            )
            st.rerun()
        except TravelApiError as error:
            st.error(str(error), icon=":material/error:")

    if st.session_state.agency_demo_pending_change:
        st.warning(
            "A new airline update is waiting. Run the monitor to see what happens.",
            icon=":material/pending_actions:",
        )
    if st.button(
        "Run monitoring check",
        icon=":material/radar:",
        type="primary" if st.session_state.agency_demo_pending_change else "secondary",
    ):
        try:
            with st.status("Running the multi-agent path…", expanded=True) as status:
                st.write("Monitoring Agent is comparing the airline revision")
                outcome = client.run_agency_demo_check(trip_id)
                st.write("Eval Agent is applying suppression and escalation rules")
                status.update(label="Monitoring check complete", state="complete")
            st.session_state.agency_demo_last_check = outcome
            st.session_state.agency_demo_pending_change = False
            st.session_state.active_trip = client.get_trip(trip_id)
            st.rerun()
        except TravelApiError as error:
            st.error(str(error), icon=":material/cloud_off:")

    with st.container(border=True):
        st.markdown("**Latest system decision**")
        _render_agency_check(st.session_state.agency_demo_last_check)

    with st.expander("Advanced manual edit"):
        with st.form("agency_manual_form", border=False):
            manual_status = st.selectbox(
                "Flight status",
                ["scheduled", "active", "landed", "cancelled", "diverted"],
                index=["scheduled", "active", "landed", "cancelled", "diverted"].index(
                    status_value if status_value in {
                        "scheduled", "active", "landed", "cancelled", "diverted"
                    } else "scheduled"
                ),
            )
            manual_delay = st.number_input(
                "Departure delay in minutes", min_value=0, max_value=720, value=delay
            )
            manual_gate = st.text_input(
                "Departure gate", value=gate if gate != "—" else "A10", max_chars=12
            )
            manual_submit = st.form_submit_button(
                "Save manual update", icon=":material/save:"
            )
        if manual_submit:
            try:
                updated = client.change_agency_flight(
                    str(flight["flight_iata"]),
                    str(flight["flight_date"]),
                    {
                        "status": manual_status,
                        "departure_delay_minutes": int(manual_delay),
                        "arrival_delay_minutes": int(manual_delay),
                        "departure_gate": manual_gate.strip() or "A10",
                        "note": "Manual operator update",
                    },
                )
                _store_agency_flight(updated)
                st.session_state.agency_demo_pending_change = True
                st.rerun()
            except TravelApiError as error:
                st.error(str(error), icon=":material/error:")


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

    if st.session_state.agency_demo_enabled and status_value != "review_required":
        _render_agency_demo(client)

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
