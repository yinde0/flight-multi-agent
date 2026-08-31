"""Explicit development views, never raw MCP arguments, URLs, or credentials."""

from __future__ import annotations

from flight_agent.flight_search_contracts import FlightSearchCommand, FlightSearchToolResult
from flight_agent.monitoring_contracts import (
    LiveFlightSample,
    ProviderFlightObservation,
    ProviderWeatherObservation,
)
from flight_agent.notification_contracts import NotificationCommand, NotificationReceipt


def flight_status_input(_client, *, flight_iata, flight_date, replay_key=None):
    return {
        "flight_iata": flight_iata,
        "flight_date": flight_date,
        "replay_requested": replay_key is not None,
    }


def flight_status_output(result: ProviderFlightObservation):
    return result.model_dump(mode="json", exclude_none=True, include={
        "source", "observed_at", "source_event_time", "status",
        "departure", "arrival", "departure_airport", "destination_airport",
        "weather", "data_freshness_seconds", "confidence",
    })


def live_sample_output(result: LiveFlightSample):
    return {
        "flight_iata": result.flight_iata,
        "flight_date": result.flight_date,
        "origin": result.origin,
        "destination": result.destination,
        "observation": flight_status_output(result.observation),
    }


def weather_input(_client, *, airport, target_at, replay_key=None):
    return {
        "airport": airport,
        "target_at": target_at,
        "replay_requested": replay_key is not None,
    }


def weather_output(result: ProviderWeatherObservation):
    return result.model_dump(mode="json", exclude_none=True, include={
        "source", "observed_at", "airport", "target_at", "forecast_at",
        "condition_code", "condition", "description", "risk_level", "alerts",
        "precipitation_probability", "wind_speed_mps", "visibility_metres", "confidence",
    })


def notification_input(_client, command: NotificationCommand):
    # Full template variables contain trip/leg IDs; recipient fields contain PII.
    # The Communication Agent separately captures its validated, PII-free wording.
    return {
        "channel": command.channel,
        "template": command.template,
        "search_requested": command.search_requested,
        "message_characters": len(command.template_variables.get("friendly_message", "")),
        "approval": {
            "verdict": command.approval.verdict,
            "policy_version": command.approval.policy_version,
            "reason_codes": command.approval.reason_codes,
        },
    }


def notification_output(result: NotificationReceipt):
    return result.model_dump(mode="json", exclude_none=True, include={
        "provider", "status", "provider_status", "delivered_at",
    })


def search_input(_client, command: FlightSearchCommand):
    view = command.model_dump(mode="json", include={
        "original_flight_iata", "origin", "destination", "departure_date",
        "earliest_departure_at", "latest_departure_at", "maximum_stops",
        "minimum_connection_minutes", "passenger_count", "cabin_class",
    })
    view["approval"] = {
        "verdict": command.approval.verdict,
        "policy_version": command.approval.policy_version,
        "reason_codes": command.approval.reason_codes,
    }
    return view


def search_output(result: FlightSearchToolResult):
    view = result.model_dump(mode="json", include={
        "provider", "source_scope", "searched_at", "availability_verified",
        "booking_guaranteed", "booking_authorized",
    })
    view["option_count"] = len(result.options)
    view["options"] = [
        option.model_dump(mode="json", exclude_none=True, include={
            "segments", "price", "offer_expires_at", "availability_status",
            "passenger_count", "live_mode",
        })
        for option in result.options
    ]
    return view
