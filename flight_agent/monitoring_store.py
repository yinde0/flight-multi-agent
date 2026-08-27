from __future__ import annotations

import json
import os
import time

from datetime import datetime, timezone
from typing import Any, Protocol

import boto3

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from flight_agent.event_delivery import candidate_outbox, confirmed_outbox


DEFAULT_TABLE_NAME = "travel-monitoring-state"


class MonitoringStore(Protocol):
    def get_last_observation(
        self, trip_id: str, leg_id: str
    ) -> dict[str, Any] | None: ...

    def put_last_observation(
        self, trip_id: str, leg_id: str, observation: dict[str, Any]
    ) -> None: ...

    def get_last_weather(
        self, trip_id: str, leg_id: str
    ) -> dict[str, Any] | None: ...

    def put_last_weather(
        self, trip_id: str, leg_id: str, weather: dict[str, Any]
    ) -> None: ...

    def put_candidate(self, candidate: dict[str, Any]) -> None: ...

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None: ...

    def put_candidate_with_outbox(self, candidate: dict[str, Any]) -> None: ...

    def get_notification(self, decision_id: str) -> dict[str, Any] | None: ...

    def put_notification(
        self, decision_id: str, notification: dict[str, Any]
    ) -> None: ...

    def wait_for_notification(
        self, decision_id: str, *, timeout_seconds: float
    ) -> dict[str, Any] | None: ...

    def get_search(self, decision_id: str) -> dict[str, Any] | None: ...

    def put_search(self, decision_id: str, search: dict[str, Any]) -> None: ...

    def wait_for_search(
        self, decision_id: str, *, timeout_seconds: float
    ) -> dict[str, Any] | None: ...

    def get_decision(self, candidate_id: str) -> dict[str, Any] | None: ...

    def put_decision(self, candidate_id: str, decision: dict[str, Any]) -> None: ...

    def get_confirmed_event(self, candidate_id: str) -> dict[str, Any] | None: ...

    def put_confirmed_event(
        self, candidate_id: str, event: dict[str, Any]
    ) -> None: ...

    def get_policy_band(self, episode_key: str) -> int | None: ...

    def put_policy_band(self, episode_key: str, band: int) -> None: ...

    def wait_for_decision(
        self, candidate_id: str, *, timeout_seconds: float
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]: ...

    def commit_evaluation_with_outbox(
        self,
        *,
        candidate_id: str,
        decision: dict[str, Any],
        confirmed_event: dict[str, Any] | None,
        episode_key: str,
        notified_band: int | None,
    ) -> None: ...

    def list_outbox(
        self, event_type: str, *, maximum: int = 20
    ) -> list[dict[str, Any]]: ...

    def delete_outbox(self, event_type: str, event_id: str) -> None: ...

    def note_outbox_failure(self, event_type: str, event_id: str) -> None: ...

    def outbox_count(self, event_type: str) -> int: ...

    def put_dead_letter(
        self,
        *,
        consumer: str,
        event_id: str,
        payload: dict[str, Any],
        error_code: str,
        attempts: int,
    ) -> None: ...

    def dead_letter_count(self, consumer: str) -> int: ...


class DynamoMonitoringStateStore:
    """DynamoDB adapter for last-known state, decisions, and policy memory."""

    def __init__(
        self,
        *,
        table_name: str = DEFAULT_TABLE_NAME,
        endpoint_url: str | None = None,
        region_name: str = "eu-west-2",
    ) -> None:
        self._table_name = table_name
        resource_kwargs: dict[str, Any] = {"region_name": region_name}
        if endpoint_url:
            resource_kwargs["endpoint_url"] = endpoint_url
            # DynamoDB Local still requires credentials syntactically. Supplying
            # inert values here also prevents accidental instance-metadata calls.
            resource_kwargs["aws_access_key_id"] = os.getenv(
                "AWS_ACCESS_KEY_ID", "local"
            )
            resource_kwargs["aws_secret_access_key"] = os.getenv(
                "AWS_SECRET_ACCESS_KEY", "local"
            )
        self._resource = boto3.resource("dynamodb", **resource_kwargs)
        self._client = self._resource.meta.client
        self._table = self._resource.Table(table_name)

    @classmethod
    def from_environment(cls) -> "DynamoMonitoringStateStore":
        return cls(
            table_name=os.getenv("MONITORING_TABLE_NAME", DEFAULT_TABLE_NAME),
            endpoint_url=os.getenv("DYNAMODB_ENDPOINT_URL") or None,
            region_name=os.getenv("AWS_REGION", "eu-west-2"),
        )

    def ensure_table(self, *, timeout_seconds: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                self._client.describe_table(TableName=self._table_name)
                return
            except self._client.exceptions.ResourceNotFoundException:
                try:
                    self._client.create_table(
                        TableName=self._table_name,
                        KeySchema=[
                            {"AttributeName": "pk", "KeyType": "HASH"},
                            {"AttributeName": "sk", "KeyType": "RANGE"},
                        ],
                        AttributeDefinitions=[
                            {"AttributeName": "pk", "AttributeType": "S"},
                            {"AttributeName": "sk", "AttributeType": "S"},
                        ],
                        BillingMode="PAY_PER_REQUEST",
                    )
                except self._client.exceptions.ResourceInUseException:
                    pass
            except (BotoCoreError, ClientError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
                continue

            if time.monotonic() >= deadline:
                raise RuntimeError("DynamoDB table was not ready before timeout")
            time.sleep(0.25)

    @staticmethod
    def _payload(item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item or not isinstance(item.get("payload"), str):
            return None
        value = json.loads(item["payload"])
        return value if isinstance(value, dict) else None

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self._table.get_item(
            Key={"pk": pk, "sk": sk}, ConsistentRead=True
        )
        return response.get("Item")

    def _put(self, pk: str, sk: str, payload: dict[str, Any]) -> None:
        self._table.put_item(
            Item=self._payload_item(pk, sk, payload)
        )

    @staticmethod
    def _payload_item(
        pk: str, sk: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "pk": pk,
            "sk": sk,
            "payload": json.dumps(
                payload, separators=(",", ":"), sort_keys=True
            ),
        }

    def _transaction_put(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self._table_name,
                # The client attached to a DynamoDB resource applies boto3's
                # native Python-to-AttributeValue transformer itself.
                "Item": item,
            }
        }

    @staticmethod
    def _outbox_partition(event_type: str) -> str:
        return f"OUTBOX#{event_type}"

    @staticmethod
    def _outbox_sort(event_id: str) -> str:
        return f"EVENT#{event_id}"

    @staticmethod
    def _outbox_item(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "pk": DynamoMonitoringStateStore._outbox_partition(
                str(record["event_type"])
            ),
            "sk": DynamoMonitoringStateStore._outbox_sort(
                str(record["event_id"])
            ),
            "payload": json.dumps(
                record, separators=(",", ":"), sort_keys=True
            ),
            "publish_attempts": 0,
        }

    @staticmethod
    def _leg_partition(trip_id: str, leg_id: str) -> str:
        return f"TRIP#{trip_id}#LEG#{leg_id}"

    def get_last_observation(
        self, trip_id: str, leg_id: str
    ) -> dict[str, Any] | None:
        return self._payload(
            self._get(self._leg_partition(trip_id, leg_id), "LAST_OBSERVATION")
        )

    def put_last_observation(
        self, trip_id: str, leg_id: str, observation: dict[str, Any]
    ) -> None:
        self._put(
            self._leg_partition(trip_id, leg_id),
            "LAST_OBSERVATION",
            observation,
        )

    def get_last_weather(
        self, trip_id: str, leg_id: str
    ) -> dict[str, Any] | None:
        return self._payload(
            self._get(self._leg_partition(trip_id, leg_id), "LAST_WEATHER")
        )

    def put_last_weather(
        self, trip_id: str, leg_id: str, weather: dict[str, Any]
    ) -> None:
        self._put(
            self._leg_partition(trip_id, leg_id),
            "LAST_WEATHER",
            weather,
        )

    def put_candidate(self, candidate: dict[str, Any]) -> None:
        self._put(
            f"TRIP#{candidate['trip_id']}",
            f"CANDIDATE#{candidate['candidate_id']}",
            candidate,
        )
        self._put(
            f"CANDIDATE#{candidate['candidate_id']}", "SOURCE", candidate
        )

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        return self._payload(
            self._get(f"CANDIDATE#{candidate_id}", "SOURCE")
        )

    def put_candidate_with_outbox(self, candidate: dict[str, Any]) -> None:
        record = candidate_outbox(candidate)
        self._client.transact_write_items(
            TransactItems=[
                self._transaction_put(
                    self._payload_item(
                        f"TRIP#{candidate['trip_id']}",
                        f"CANDIDATE#{candidate['candidate_id']}",
                        candidate,
                    )
                ),
                self._transaction_put(
                    self._payload_item(
                        f"CANDIDATE#{candidate['candidate_id']}",
                        "SOURCE",
                        candidate,
                    )
                ),
                self._transaction_put(self._outbox_item(record)),
            ]
        )

    def get_notification(self, decision_id: str) -> dict[str, Any] | None:
        return self._payload(self._get(f"DECISION#{decision_id}", "NOTIFICATION"))

    def put_notification(
        self, decision_id: str, notification: dict[str, Any]
    ) -> None:
        self._put(f"DECISION#{decision_id}", "NOTIFICATION", notification)

    def wait_for_notification(
        self, decision_id: str, *, timeout_seconds: float
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            notification = self.get_notification(decision_id)
            if notification is not None:
                return notification
            time.sleep(0.05)
        return None

    def get_search(self, decision_id: str) -> dict[str, Any] | None:
        return self._payload(self._get(f"DECISION#{decision_id}", "SEARCH"))

    def put_search(self, decision_id: str, search: dict[str, Any]) -> None:
        self._put(f"DECISION#{decision_id}", "SEARCH", search)

    def wait_for_search(
        self, decision_id: str, *, timeout_seconds: float
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            search = self.get_search(decision_id)
            if search is not None:
                return search
            time.sleep(0.05)
        return None

    def get_decision(self, candidate_id: str) -> dict[str, Any] | None:
        return self._payload(self._get(f"CANDIDATE#{candidate_id}", "DECISION"))

    def put_decision(self, candidate_id: str, decision: dict[str, Any]) -> None:
        self._put(f"CANDIDATE#{candidate_id}", "DECISION", decision)

    def commit_evaluation_with_outbox(
        self,
        *,
        candidate_id: str,
        decision: dict[str, Any],
        confirmed_event: dict[str, Any] | None,
        episode_key: str,
        notified_band: int | None,
    ) -> None:
        items: list[dict[str, Any]] = []
        if confirmed_event is not None:
            items.append(
                self._transaction_put(
                    self._payload_item(
                        f"CANDIDATE#{candidate_id}",
                        "CONFIRMED_EVENT",
                        confirmed_event,
                    )
                )
            )
        if episode_key and notified_band is not None:
            items.append(
                self._transaction_put(
                    {
                        "pk": f"POLICY#{episode_key}",
                        "sk": "HIGHEST_NOTIFIED_BAND",
                        "band": notified_band,
                    }
                )
            )
        items.append(
            self._transaction_put(
                self._payload_item(
                    f"CANDIDATE#{candidate_id}", "DECISION", decision
                )
            )
        )
        if confirmed_event is not None:
            items.append(
                self._transaction_put(
                    self._outbox_item(confirmed_outbox(confirmed_event))
                )
            )
        self._client.transact_write_items(TransactItems=items)

    def get_confirmed_event(self, candidate_id: str) -> dict[str, Any] | None:
        return self._payload(
            self._get(f"CANDIDATE#{candidate_id}", "CONFIRMED_EVENT")
        )

    def put_confirmed_event(
        self, candidate_id: str, event: dict[str, Any]
    ) -> None:
        self._put(f"CANDIDATE#{candidate_id}", "CONFIRMED_EVENT", event)

    def get_policy_band(self, episode_key: str) -> int | None:
        item = self._get(f"POLICY#{episode_key}", "HIGHEST_NOTIFIED_BAND")
        if not item or "band" not in item:
            return None
        return int(item["band"])

    def put_policy_band(self, episode_key: str, band: int) -> None:
        self._table.put_item(
            Item={
                "pk": f"POLICY#{episode_key}",
                "sk": "HIGHEST_NOTIFIED_BAND",
                "band": band,
            }
        )

    def wait_for_decision(
        self, candidate_id: str, *, timeout_seconds: float
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            decision = self.get_decision(candidate_id)
            if decision is not None:
                return decision, self.get_confirmed_event(candidate_id)
            time.sleep(0.05)
        return None, None

    def list_outbox(
        self, event_type: str, *, maximum: int = 20
    ) -> list[dict[str, Any]]:
        response = self._table.query(
            KeyConditionExpression=Key("pk").eq(
                self._outbox_partition(event_type)
            ),
            Limit=maximum,
            ConsistentRead=True,
        )
        records: list[dict[str, Any]] = []
        for item in response.get("Items", []):
            payload = self._payload(item)
            if payload is not None:
                records.append(payload)
        return records

    def delete_outbox(self, event_type: str, event_id: str) -> None:
        self._table.delete_item(
            Key={
                "pk": self._outbox_partition(event_type),
                "sk": self._outbox_sort(event_id),
            }
        )

    def note_outbox_failure(self, event_type: str, event_id: str) -> None:
        self._table.update_item(
            Key={
                "pk": self._outbox_partition(event_type),
                "sk": self._outbox_sort(event_id),
            },
            UpdateExpression=(
                "ADD publish_attempts :one SET last_attempt_at = :attempted"
            ),
            ExpressionAttributeValues={
                ":one": 1,
                ":attempted": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            },
        )

    def outbox_count(self, event_type: str) -> int:
        response = self._table.query(
            KeyConditionExpression=Key("pk").eq(
                self._outbox_partition(event_type)
            ),
            Select="COUNT",
            ConsistentRead=True,
        )
        return int(response.get("Count", 0))

    def put_dead_letter(
        self,
        *,
        consumer: str,
        event_id: str,
        payload: dict[str, Any],
        error_code: str,
        attempts: int,
    ) -> None:
        self._table.put_item(
            Item={
                "pk": f"DEADLETTER#{consumer}",
                "sk": f"EVENT#{event_id}",
                "payload": json.dumps(
                    payload, separators=(",", ":"), sort_keys=True
                ),
                "error_code": error_code,
                "attempts": attempts,
                "recorded_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            }
        )

    def dead_letter_count(self, consumer: str) -> int:
        response = self._table.query(
            KeyConditionExpression=Key("pk").eq(f"DEADLETTER#{consumer}"),
            Select="COUNT",
            ConsistentRead=True,
        )
        return int(response.get("Count", 0))
