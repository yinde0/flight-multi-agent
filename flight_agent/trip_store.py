from __future__ import annotations

import os

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import psycopg

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from flight_agent.contracts import CanonicalItinerary
from flight_agent.trip_contracts import (
    DocumentObjectRef,
    NotificationRecipient,
    ScheduledLeg,
    SmsNotificationPreference,
    StoredLegView,
    StoredTripView,
)
from travel_eval.clock import parse_timestamp


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def format_poll_identity(value: datetime) -> str:
    """Preserve database precision for the optimistic poll-completion guard."""

    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def next_poll_time(
    *, scheduled_departure_at: str, scheduled_arrival_at: str, completed_at: datetime
) -> tuple[str, datetime | None]:
    departure = parse_timestamp(scheduled_departure_at)
    arrival = parse_timestamp(scheduled_arrival_at)
    remaining = departure - completed_at
    if completed_at >= arrival + timedelta(hours=2):
        return "completed", None
    if remaining > timedelta(hours=72):
        interval = timedelta(hours=6)
    elif remaining > timedelta(hours=24):
        interval = timedelta(hours=2)
    elif remaining > timedelta(hours=6):
        interval = timedelta(minutes=30)
    else:
        interval = timedelta(minutes=10)
    return "active", completed_at + interval


class TripStore(Protocol):
    def ensure_schema(self) -> None: ...

    def get_trip(self, trip_id: str) -> StoredTripView | None: ...

    def get_notification_recipient(
        self, trip_id: str
    ) -> NotificationRecipient | None: ...

    def save_parsed_trip(
        self,
        itinerary: CanonicalItinerary,
        document: DocumentObjectRef,
        *,
        created_at: datetime,
        notification_preference: SmsNotificationPreference | None = None,
    ) -> bool: ...

    def save_review_trip(
        self,
        *,
        trip_id: str,
        traveler_ref: str,
        document: DocumentObjectRef,
        review: dict[str, Any],
        created_at: datetime,
        notification_preference: SmsNotificationPreference | None = None,
    ) -> bool: ...

    def put_trace_context(
        self, trip_id: str, trace_headers: dict[str, str]
    ) -> None: ...

    def claim_due_legs(
        self,
        *,
        now: datetime,
        maximum_legs: int,
        lease_seconds: int,
        trip_id: str | None = None,
    ) -> list[ScheduledLeg]: ...

    def complete_poll(
        self,
        leg: ScheduledLeg,
        outcome: dict[str, Any],
        *,
        completed_at: datetime,
    ) -> None: ...

    def fail_poll(
        self, leg: ScheduledLeg, *, error_code: str, failed_at: datetime
    ) -> None: ...


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trips (
    trip_id TEXT PRIMARY KEY,
    traveler_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'review_required', 'completed')),
    document_bucket TEXT NOT NULL,
    document_key TEXT NOT NULL,
    document_sha256 CHAR(64) NOT NULL,
    document_etag TEXT,
    itinerary_json JSONB,
    review_json JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS trip_notification_contacts (
    trip_id TEXT PRIMARY KEY REFERENCES trips(trip_id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel = 'sms'),
    phone_e164 TEXT NOT NULL CHECK (phone_e164 ~ '^\\+[1-9][0-9]{7,14}$'),
    consent_granted_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS trip_legs (
    trip_id TEXT NOT NULL REFERENCES trips(trip_id) ON DELETE CASCADE,
    leg_id TEXT NOT NULL,
    flight_iata TEXT NOT NULL,
    flight_date DATE NOT NULL,
    origin CHAR(3) NOT NULL,
    destination CHAR(3) NOT NULL,
    scheduled_departure_at TIMESTAMPTZ NOT NULL,
    scheduled_arrival_at TIMESTAMPTZ NOT NULL,
    monitoring_status TEXT NOT NULL CHECK (monitoring_status IN ('active', 'completed')),
    next_poll_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    last_poll_at TIMESTAMPTZ,
    poll_count INTEGER NOT NULL DEFAULT 0,
    last_poll_status TEXT,
    PRIMARY KEY (trip_id, leg_id)
);

CREATE INDEX IF NOT EXISTS trip_legs_due_idx
ON trip_legs (next_poll_at)
WHERE monitoring_status = 'active';

CREATE TABLE IF NOT EXISTS monitoring_runs (
    poll_key TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL,
    leg_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    outcome_json JSONB,
    error_code TEXT,
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS trip_trace_contexts (
    trip_id TEXT PRIMARY KEY REFERENCES trips(trip_id) ON DELETE CASCADE,
    trace_headers_json JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
"""


class PostgresTripStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    @classmethod
    def from_environment(cls) -> "PostgresTripStore":
        return cls(
            os.getenv(
                "DATABASE_URL",
                "postgresql://travel:travel-local@127.0.0.1:5432/travel",
            )
        )

    def _connect(self):
        return psycopg.connect(self._database_url, row_factory=dict_row)

    def ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(SCHEMA_SQL)

    @staticmethod
    def _document(row: dict[str, Any]) -> DocumentObjectRef:
        return DocumentObjectRef(
            bucket=row["document_bucket"],
            key=row["document_key"],
            sha256=row["document_sha256"],
            etag=row["document_etag"],
        )

    def get_trip(self, trip_id: str) -> StoredTripView | None:
        with self._connect() as connection:
            trip = connection.execute(
                "SELECT * FROM trips WHERE trip_id = %s", (trip_id,)
            ).fetchone()
            if trip is None:
                return None
            legs = connection.execute(
                """
                SELECT leg_id, flight_iata, origin, destination, monitoring_status,
                       next_poll_at, last_poll_at, poll_count, last_poll_status
                FROM trip_legs WHERE trip_id = %s ORDER BY scheduled_departure_at, leg_id
                """,
                (trip_id,),
            ).fetchall()
        itinerary = (
            CanonicalItinerary.model_validate(trip["itinerary_json"])
            if trip["itinerary_json"] is not None
            else None
        )
        return StoredTripView(
            trip_id=trip["trip_id"],
            traveler_ref=trip["traveler_ref"],
            status=trip["status"],
            document=self._document(trip),
            itinerary=itinerary,
            review=trip["review_json"],
            legs=[
                StoredLegView(
                    leg_id=leg["leg_id"],
                    flight_iata=leg["flight_iata"],
                    origin=str(leg["origin"]).strip(),
                    destination=str(leg["destination"]).strip(),
                    monitoring_status=leg["monitoring_status"],
                    next_poll_at=(
                        format_timestamp(leg["next_poll_at"])
                        if leg["next_poll_at"] is not None
                        else None
                    ),
                    last_poll_at=(
                        format_timestamp(leg["last_poll_at"])
                        if leg["last_poll_at"] is not None
                        else None
                    ),
                    poll_count=leg["poll_count"],
                    last_poll_status=leg["last_poll_status"],
                )
                for leg in legs
            ],
            created_at=format_timestamp(trip["created_at"]),
            updated_at=format_timestamp(trip["updated_at"]),
        )

    def get_notification_recipient(
        self, trip_id: str
    ) -> NotificationRecipient | None:
        with self._connect() as connection:
            contact = connection.execute(
                """
                SELECT trip_id, phone_e164, consent_granted_at
                FROM trip_notification_contacts
                WHERE trip_id = %s AND channel = 'sms'
                """,
                (trip_id,),
            ).fetchone()
        if contact is None:
            return None
        return NotificationRecipient(
            trip_id=contact["trip_id"],
            recipient_ref=f"traveler:{contact['trip_id']}",
            phone_e164=contact["phone_e164"],
            consent_granted_at=format_timestamp(contact["consent_granted_at"]),
        )

    @staticmethod
    def _save_notification_preference(
        connection,
        *,
        trip_id: str,
        preference: SmsNotificationPreference | None,
        created_at: datetime,
    ) -> None:
        if preference is None:
            return
        connection.execute(
            """
            INSERT INTO trip_notification_contacts (
                trip_id, channel, phone_e164, consent_granted_at,
                created_at, updated_at
            ) VALUES (%s, 'sms', %s, %s, %s, %s)
            """,
            (
                trip_id,
                preference.phone_e164,
                parse_timestamp(preference.consent_granted_at),
                created_at,
                created_at,
            ),
        )

    def save_parsed_trip(
        self,
        itinerary: CanonicalItinerary,
        document: DocumentObjectRef,
        *,
        created_at: datetime,
        notification_preference: SmsNotificationPreference | None = None,
    ) -> bool:
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT INTO trips (
                    trip_id, traveler_ref, status, document_bucket, document_key,
                    document_sha256, document_etag, itinerary_json, review_json,
                    created_at, updated_at
                ) VALUES (%s, %s, 'active', %s, %s, %s, %s, %s, NULL, %s, %s)
                ON CONFLICT (trip_id) DO NOTHING
                RETURNING trip_id
                """,
                (
                    itinerary.trip_id,
                    itinerary.traveler_ref,
                    document.bucket,
                    document.key,
                    document.sha256,
                    document.etag,
                    Jsonb(itinerary.model_dump(mode="json")),
                    created_at,
                    created_at,
                ),
            ).fetchone()
            if inserted is None:
                return False
            for leg in itinerary.legs:
                departure = parse_timestamp(leg.scheduled_departure_at)
                connection.execute(
                    """
                    INSERT INTO trip_legs (
                        trip_id, leg_id, flight_iata, flight_date, origin,
                        destination, scheduled_departure_at, scheduled_arrival_at,
                        monitoring_status, next_poll_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
                    """,
                    (
                        itinerary.trip_id,
                        leg.leg_id,
                        leg.flight_number,
                        departure.date(),
                        leg.origin,
                        leg.destination,
                        departure,
                        parse_timestamp(leg.scheduled_arrival_at),
                        created_at,
                    ),
                )
            self._save_notification_preference(
                connection,
                trip_id=itinerary.trip_id,
                preference=notification_preference,
                created_at=created_at,
            )
        return True

    def save_review_trip(
        self,
        *,
        trip_id: str,
        traveler_ref: str,
        document: DocumentObjectRef,
        review: dict[str, Any],
        created_at: datetime,
        notification_preference: SmsNotificationPreference | None = None,
    ) -> bool:
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT INTO trips (
                    trip_id, traveler_ref, status, document_bucket, document_key,
                    document_sha256, document_etag, itinerary_json, review_json,
                    created_at, updated_at
                ) VALUES (%s, %s, 'review_required', %s, %s, %s, %s, NULL, %s, %s, %s)
                ON CONFLICT (trip_id) DO NOTHING
                RETURNING trip_id
                """,
                (
                    trip_id,
                    traveler_ref,
                    document.bucket,
                    document.key,
                    document.sha256,
                    document.etag,
                    Jsonb(review),
                    created_at,
                    created_at,
                ),
            ).fetchone()
            if inserted is not None:
                self._save_notification_preference(
                    connection,
                    trip_id=trip_id,
                    preference=notification_preference,
                    created_at=created_at,
                )
        return inserted is not None

    def put_trace_context(
        self, trip_id: str, trace_headers: dict[str, str]
    ) -> None:
        if not trace_headers:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trip_trace_contexts (
                    trip_id, trace_headers_json, updated_at
                ) VALUES (%s, %s, NOW())
                ON CONFLICT (trip_id) DO UPDATE
                SET trace_headers_json = EXCLUDED.trace_headers_json,
                    updated_at = EXCLUDED.updated_at
                """,
                (trip_id, Jsonb(trace_headers)),
            )

    def claim_due_legs(
        self,
        *,
        now: datetime,
        maximum_legs: int,
        lease_seconds: int,
        trip_id: str | None = None,
    ) -> list[ScheduledLeg]:
        lease_until = now + timedelta(seconds=lease_seconds)
        trip_filter = ""
        parameters: list[Any] = [now, now]
        if trip_id is not None:
            trip_filter = "AND trip_id = %s"
            parameters.append(trip_id)
        parameters.extend([maximum_legs, lease_until])
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                WITH due AS (
                    SELECT trip_id, leg_id
                    FROM trip_legs
                    WHERE monitoring_status = 'active'
                      AND next_poll_at <= %s
                      AND (lease_until IS NULL OR lease_until <= %s)
                      {trip_filter}
                    ORDER BY next_poll_at, trip_id, leg_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE trip_legs AS leg
                SET lease_until = %s
                FROM due
                WHERE leg.trip_id = due.trip_id AND leg.leg_id = due.leg_id
                RETURNING leg.*
                """,
                tuple(parameters),
            ).fetchall()
            trip_ids = sorted({str(row["trip_id"]) for row in rows})
            contexts: dict[str, dict[str, str]] = {}
            if trip_ids:
                trace_rows = connection.execute(
                    """
                    SELECT trip_id, trace_headers_json
                    FROM trip_trace_contexts
                    WHERE trip_id = ANY(%s)
                    """,
                    (trip_ids,),
                ).fetchall()
                contexts = {
                    str(row["trip_id"]): dict(row["trace_headers_json"] or {})
                    for row in trace_rows
                }
        return [
            ScheduledLeg(
                trip_id=row["trip_id"],
                leg_id=row["leg_id"],
                flight_iata=row["flight_iata"],
                flight_date=row["flight_date"].isoformat(),
                scheduled_departure_at=format_timestamp(
                    row["scheduled_departure_at"]
                ),
                scheduled_arrival_at=format_timestamp(row["scheduled_arrival_at"]),
                # Unlike display timestamps, this value is parsed and compared back
                # to Postgres when the worker completes. Dropping microseconds can
                # make the guarded UPDATE miss a freshly activated leg.
                due_at=format_poll_identity(row["next_poll_at"]),
                replay_key=f"scheduled:{row['trip_id']}:{row['leg_id']}",
                trace_headers=contexts.get(str(row["trip_id"]), {}),
            )
            for row in rows
        ]

    def complete_poll(
        self,
        leg: ScheduledLeg,
        outcome: dict[str, Any],
        *,
        completed_at: datetime,
    ) -> None:
        status, next_at = next_poll_time(
            scheduled_departure_at=leg.scheduled_departure_at,
            scheduled_arrival_at=leg.scheduled_arrival_at,
            completed_at=completed_at,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO monitoring_runs (
                    poll_key, trip_id, leg_id, status, outcome_json, error_code,
                    recorded_at
                ) VALUES (%s, %s, %s, 'completed', %s, NULL, %s)
                ON CONFLICT (poll_key) DO NOTHING
                """,
                (
                    leg.poll_key,
                    leg.trip_id,
                    leg.leg_id,
                    Jsonb(outcome),
                    completed_at,
                ),
            )
            connection.execute(
                """
                UPDATE trip_legs
                SET monitoring_status = %s, next_poll_at = %s, lease_until = NULL,
                    last_poll_at = %s, poll_count = poll_count + 1,
                    last_poll_status = %s
                WHERE trip_id = %s AND leg_id = %s AND next_poll_at = %s
                """,
                (
                    status,
                    next_at,
                    completed_at,
                    str(outcome.get("status") or "unknown"),
                    leg.trip_id,
                    leg.leg_id,
                    parse_timestamp(leg.due_at),
                ),
            )
            connection.execute(
                """
                UPDATE trips SET status = 'completed', updated_at = %s
                WHERE trip_id = %s
                  AND NOT EXISTS (
                    SELECT 1 FROM trip_legs
                    WHERE trip_id = %s AND monitoring_status = 'active'
                  )
                """,
                (completed_at, leg.trip_id, leg.trip_id),
            )
            connection.execute(
                "UPDATE trips SET updated_at = %s WHERE trip_id = %s",
                (completed_at, leg.trip_id),
            )

    def fail_poll(
        self, leg: ScheduledLeg, *, error_code: str, failed_at: datetime
    ) -> None:
        retry_at = failed_at + timedelta(minutes=5)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO monitoring_runs (
                    poll_key, trip_id, leg_id, status, outcome_json, error_code,
                    recorded_at
                ) VALUES (%s, %s, %s, 'failed', NULL, %s, %s)
                ON CONFLICT (poll_key) DO NOTHING
                """,
                (leg.poll_key, leg.trip_id, leg.leg_id, error_code, failed_at),
            )
            connection.execute(
                """
                UPDATE trip_legs
                SET next_poll_at = %s, lease_until = NULL, last_poll_at = %s,
                    last_poll_status = 'poll_failed'
                WHERE trip_id = %s AND leg_id = %s AND next_poll_at = %s
                """,
                (
                    retry_at,
                    failed_at,
                    leg.trip_id,
                    leg.leg_id,
                    parse_timestamp(leg.due_at),
                ),
            )
