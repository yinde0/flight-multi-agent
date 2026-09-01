from __future__ import annotations

import asyncio
import json
import os
import time

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol

import boto3
import nats


class EventBusError(RuntimeError):
    """The configured durable event transport is unavailable or invalid."""


class EventSubscription(Protocol):
    async def unsubscribe(self) -> None: ...


class EventBus(Protocol):
    provider_name: str

    def jetstream(self) -> "EventBus": ...

    async def ensure(self) -> None: ...

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str] | None = None,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> Any: ...

    async def subscribe(
        self,
        subject: str,
        *,
        durable_name: str,
        callback: Any,
        **kwargs: Any,
    ) -> EventSubscription: ...

    async def drain(self) -> None: ...


class NatsEventBus:
    """JetStream-backed local event bus."""

    provider_name = "nats"

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._jetstream = connection.jetstream()

    @classmethod
    async def connect(
        cls, url: str, *, timeout_seconds: float = 30.0
    ) -> "NatsEventBus":
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            try:
                connection = await nats.connect(
                    servers=[url],
                    connect_timeout=2,
                    max_reconnect_attempts=10,
                )
                return cls(connection)
            except Exception:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(0.5)

    def jetstream(self) -> "NatsEventBus":
        return self

    async def ensure(self) -> None:
        # Stream creation is performed by event_delivery using the shared schema.
        return None

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str] | None = None,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        kwargs.pop("target_consumer", None)
        resolved_headers = dict(headers or {})
        if message_id:
            resolved_headers["Nats-Msg-Id"] = message_id
        return await self._jetstream.publish(
            subject, payload, headers=resolved_headers, **kwargs
        )

    async def subscribe(
        self,
        subject: str,
        *,
        durable_name: str,
        callback: Any,
        **kwargs: Any,
    ) -> EventSubscription:
        return await self._jetstream.subscribe(
            subject,
            queue=durable_name,
            durable=durable_name,
            cb=callback,
            **kwargs,
        )

    async def stream_info(self, name: str) -> Any:
        return await self._jetstream.stream_info(name)

    async def consumer_info(self, stream: str, consumer: str) -> Any:
        return await self._jetstream.consumer_info(stream, consumer)

    async def add_stream(self, *, config: Any) -> Any:
        return await self._jetstream.add_stream(config=config)

    async def drain(self) -> None:
        await self._connection.drain()


class SqsIdempotencyStore:
    """DynamoDB claims prevent the same event ID running twice per consumer."""

    def __init__(self, table_name: str, *, client: Any | None = None) -> None:
        self._table_name = table_name
        self._client = client or boto3.client(
            "dynamodb", region_name=os.getenv("AWS_REGION") or None
        )
        self._lease_seconds = int(os.getenv("SQS_IDEMPOTENCY_LEASE_SECONDS", "120"))
        self._ttl_seconds = int(os.getenv("SQS_IDEMPOTENCY_TTL_SECONDS", "1209600"))

    def claim(self, consumer: str, event_id: str) -> str:
        key = f"{consumer}#{event_id}"
        now = int(time.time())
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item={
                    "event_key": {"S": key},
                    "status": {"S": "processing"},
                    "lease_expires_at": {"N": str(now + self._lease_seconds)},
                    "expires_at": {"N": str(now + self._ttl_seconds)},
                },
                ConditionExpression=(
                    "attribute_not_exists(event_key) OR lease_expires_at < :now"
                ),
                ExpressionAttributeValues={":now": {"N": str(now)}},
            )
            return "claimed"
        except self._client.exceptions.ConditionalCheckFailedException:
            response = self._client.get_item(
                TableName=self._table_name,
                Key={"event_key": {"S": key}},
                ConsistentRead=True,
            )
            status = response.get("Item", {}).get("status", {}).get("S")
            return "complete" if status == "complete" else "busy"

    def complete(self, consumer: str, event_id: str) -> None:
        now = int(time.time())
        self._client.update_item(
            TableName=self._table_name,
            Key={"event_key": {"S": f"{consumer}#{event_id}"}},
            UpdateExpression="SET #status = :complete, expires_at = :expires REMOVE lease_expires_at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":complete": {"S": "complete"},
                ":expires": {"N": str(now + self._ttl_seconds)},
            },
        )

    def release(self, consumer: str, event_id: str) -> None:
        self._client.delete_item(
            TableName=self._table_name,
            Key={"event_key": {"S": f"{consumer}#{event_id}"}},
            ConditionExpression="#status = :processing",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":processing": {"S": "processing"}},
        )


class _MemoryIdempotencyStore:
    def __init__(self) -> None:
        self._states: dict[str, str] = {}
        self._lock = __import__("threading").Lock()

    def claim(self, consumer: str, event_id: str) -> str:
        key = f"{consumer}#{event_id}"
        with self._lock:
            if key not in self._states:
                self._states[key] = "processing"
                return "claimed"
            return "complete" if self._states[key] == "complete" else "busy"

    def complete(self, consumer: str, event_id: str) -> None:
        with self._lock:
            self._states[f"{consumer}#{event_id}"] = "complete"

    def release(self, consumer: str, event_id: str) -> None:
        with self._lock:
            self._states.pop(f"{consumer}#{event_id}", None)


class SqsMessage:
    def __init__(
        self,
        *,
        bus: "SqsEventBus",
        queue_url: str,
        consumer: str,
        raw: dict[str, Any],
        event_id: str,
    ) -> None:
        self._bus = bus
        self._queue_url = queue_url
        self._consumer = consumer
        self._raw = raw
        self._event_id = event_id
        self.data = str(raw["Body"]).encode("utf-8")
        self.headers = {
            key: str(value.get("StringValue") or "")
            for key, value in raw.get("MessageAttributes", {}).items()
            if value.get("DataType") == "String"
        }
        attempt = raw.get("Attributes", {}).get("ApproximateReceiveCount", "1")
        self.metadata = SimpleNamespace(num_delivered=max(1, int(attempt)))
        self._settled = False

    async def ack_sync(self, timeout: float | None = None) -> None:
        del timeout
        if self._settled:
            return
        await asyncio.to_thread(
            self._bus._idempotency.complete, self._consumer, self._event_id
        )
        await asyncio.to_thread(
            self._bus._client.delete_message,
            QueueUrl=self._queue_url,
            ReceiptHandle=self._raw["ReceiptHandle"],
        )
        self._settled = True

    async def nak(self, delay: float | None = None) -> None:
        if self._settled:
            return
        await asyncio.to_thread(
            self._bus._idempotency.release, self._consumer, self._event_id
        )
        await self.extend_visibility(
            int(delay if delay is not None else self._bus._retry_delay)
        )
        self._settled = True

    async def term(self) -> None:
        if self._settled:
            return
        dead_letter_url = self._bus.dead_letter_url(self._consumer)
        if dead_letter_url:
            arguments = {
                "QueueUrl": dead_letter_url,
                "MessageBody": self._raw["Body"],
                "MessageAttributes": self._raw.get("MessageAttributes", {}),
            }
            if dead_letter_url.endswith(".fifo"):
                arguments.update(
                    MessageDeduplicationId=self._event_id,
                    MessageGroupId=self._consumer[:128],
                )
            await asyncio.to_thread(self._bus._client.send_message, **arguments)
        # Quarantine is not successful processing. Release the event-ID claim
        # so an operator-approved DLQ redrive can execute the same event again.
        await asyncio.to_thread(
            self._bus._idempotency.release, self._consumer, self._event_id
        )
        await asyncio.to_thread(
            self._bus._client.delete_message,
            QueueUrl=self._queue_url,
            ReceiptHandle=self._raw["ReceiptHandle"],
        )
        self._settled = True

    async def extend_visibility(self, seconds: int | None = None) -> None:
        if self._settled:
            return
        await asyncio.to_thread(
            self._bus._client.change_message_visibility,
            QueueUrl=self._queue_url,
            ReceiptHandle=self._raw["ReceiptHandle"],
            VisibilityTimeout=max(0, int(seconds or self._bus._visibility_timeout)),
        )

    async def in_progress(self) -> None:
        await self.extend_visibility()


@dataclass
class _SqsSubscription:
    stop: asyncio.Event
    task: asyncio.Task[Any]

    async def unsubscribe(self) -> None:
        self.stop.set()
        await self.task


class SqsEventBus:
    """SQS transport with fan-out queues, long polling, retries, and DLQs."""

    provider_name = "sqs"

    def __init__(
        self,
        *,
        client: Any | None = None,
        queue_urls: dict[str, str] | None = None,
        dead_letter_urls: dict[str, str] | None = None,
        idempotency: Any | None = None,
    ) -> None:
        self._client = client or boto3.client(
            "sqs", region_name=os.getenv("AWS_REGION") or None
        )
        self._queue_urls = queue_urls or self._queue_urls_from_environment()
        self._dead_letter_urls = (
            dead_letter_urls or self._dead_letter_urls_from_environment()
        )
        table = os.getenv("SQS_IDEMPOTENCY_TABLE", "").strip()
        if idempotency is not None:
            self._idempotency = idempotency
        elif table:
            self._idempotency = SqsIdempotencyStore(table)
        else:
            self._idempotency = _MemoryIdempotencyStore()
        self._long_poll = min(20, max(1, int(os.getenv("SQS_LONG_POLL_SECONDS", "20"))))
        self._visibility_timeout = int(os.getenv("SQS_VISIBILITY_TIMEOUT_SECONDS", "60"))
        self._retry_delay = int(os.getenv("EVENT_RETRY_DELAY_SECONDS", "2"))
        self._subscriptions: list[_SqsSubscription] = []

    @staticmethod
    def _queue_urls_from_environment() -> dict[str, str]:
        raw = os.getenv("SQS_QUEUE_URLS_JSON", "").strip()
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise EventBusError("SQS_QUEUE_URLS_JSON must be an object")
            return {str(key): str(value) for key, value in parsed.items()}
        candidates = {
            "travel.disruption_candidate.v1|travel-eval-agent-v1": os.getenv(
                "SQS_QUEUE_URL_DISRUPTION_CANDIDATE", ""
            ),
            "travel.disruption_confirmed.v1|travel-notification-action-v1": os.getenv(
                "SQS_QUEUE_URL_NOTIFICATION", ""
            ),
            "travel.disruption_confirmed.v1|travel-flight-search-action-v1": os.getenv(
                "SQS_QUEUE_URL_FLIGHT_SEARCH", ""
            ),
        }
        return {key: value for key, value in candidates.items() if value}

    @staticmethod
    def _dead_letter_urls_from_environment() -> dict[str, str]:
        raw = os.getenv("SQS_DLQ_URLS_JSON", "").strip()
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise EventBusError("SQS_DLQ_URLS_JSON must be an object")
            return {str(key): str(value) for key, value in parsed.items()}
        candidates = {
            "travel-eval-agent-v1": os.getenv("SQS_DLQ_URL_EVAL", ""),
            "travel-notification-action-v1": os.getenv(
                "SQS_DLQ_URL_NOTIFICATION", ""
            ),
            "travel-flight-search-action-v1": os.getenv(
                "SQS_DLQ_URL_FLIGHT_SEARCH", ""
            ),
        }
        return {key: value for key, value in candidates.items() if value}

    @classmethod
    async def connect(cls, **kwargs: Any) -> "SqsEventBus":
        return cls(**kwargs)

    def jetstream(self) -> "SqsEventBus":
        return self

    async def ensure(self) -> None:
        required = {
            "travel.disruption_candidate.v1|travel-eval-agent-v1",
            "travel.disruption_confirmed.v1|travel-notification-action-v1",
            "travel.disruption_confirmed.v1|travel-flight-search-action-v1",
        }
        missing = sorted(required - self._queue_urls.keys())
        if missing:
            raise EventBusError("Missing SQS queue mappings: " + ", ".join(missing))
        if os.getenv("DEPLOYMENT_ENVIRONMENT", "development") != "development" and isinstance(
            self._idempotency, _MemoryIdempotencyStore
        ):
            raise EventBusError("SQS_IDEMPOTENCY_TABLE is required outside development")

    def _destinations(self, subject: str) -> list[str]:
        return sorted(
            {url for key, url in self._queue_urls.items() if key.startswith(subject + "|")}
        )

    def _queue_url(self, subject: str, consumer: str) -> str:
        try:
            return self._queue_urls[f"{subject}|{consumer}"]
        except KeyError as error:
            raise EventBusError(
                f"No SQS queue is mapped for {subject} and {consumer}"
            ) from error

    def dead_letter_url(self, consumer: str) -> str | None:
        return self._dead_letter_urls.get(consumer)

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str] | None = None,
        message_id: str | None = None,
        target_consumer: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        destinations = (
            [self._queue_url(subject, target_consumer)]
            if target_consumer
            else self._destinations(subject)
        )
        if not destinations:
            raise EventBusError(f"No SQS destinations are mapped for {subject}")
        body = payload.decode("utf-8")
        envelope = json.loads(body)
        event_id = str(message_id or envelope.get("event_id") or "")
        if not event_id:
            raise EventBusError("Every SQS event requires an event_id")
        attributes = {
            key: {"DataType": "String", "StringValue": str(value)}
            for key, value in {**(headers or {}), "event_id": event_id, "subject": subject}.items()
        }
        responses = []
        for queue_url in destinations:
            arguments: dict[str, Any] = {
                "QueueUrl": queue_url,
                "MessageBody": body,
                "MessageAttributes": attributes,
            }
            if queue_url.endswith(".fifo"):
                group = str(envelope.get("payload", {}).get("trip_id") or subject)
                arguments.update(
                    MessageDeduplicationId=event_id,
                    MessageGroupId=group[:128],
                )
            responses.append(
                await asyncio.to_thread(self._client.send_message, **arguments)
            )
        return {"destinations": len(responses), "responses": responses}

    async def subscribe(
        self,
        subject: str,
        *,
        durable_name: str,
        callback: Any,
        **kwargs: Any,
    ) -> EventSubscription:
        del kwargs
        queue_url = self._queue_url(subject, durable_name)
        stop = asyncio.Event()
        task = asyncio.create_task(
            self._poll(queue_url, durable_name, callback, stop)
        )
        subscription = _SqsSubscription(stop=stop, task=task)
        self._subscriptions.append(subscription)
        return subscription

    async def _poll(
        self,
        queue_url: str,
        consumer: str,
        callback: Any,
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                response = await asyncio.to_thread(
                    self._client.receive_message,
                    QueueUrl=queue_url,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=self._long_poll,
                    VisibilityTimeout=self._visibility_timeout,
                    AttributeNames=["ApproximateReceiveCount"],
                    MessageAttributeNames=["All"],
                )
            except Exception:
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=max(1, self._retry_delay)
                    )
                except TimeoutError:
                    continue
                return
            for raw in response.get("Messages", []):
                try:
                    envelope = json.loads(raw["Body"])
                    event_id = str(envelope["event_id"])
                except (KeyError, TypeError, ValueError):
                    event_id = "unreadable-" + str(raw.get("MessageId") or "unknown")
                claim = await asyncio.to_thread(
                    self._idempotency.claim, consumer, event_id
                )
                message = SqsMessage(
                    bus=self,
                    queue_url=queue_url,
                    consumer=consumer,
                    raw=raw,
                    event_id=event_id,
                )
                if claim == "complete":
                    await message.ack_sync()
                    continue
                if claim == "busy":
                    await message.extend_visibility(self._retry_delay)
                    continue
                heartbeat = asyncio.create_task(self._heartbeat(message, stop))
                try:
                    await callback(message)
                except Exception:
                    await message.nak(self._retry_delay)
                finally:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass

    async def _heartbeat(self, message: SqsMessage, stop: asyncio.Event) -> None:
        interval = max(1, self._visibility_timeout // 2)
        while not stop.is_set() and not message._settled:
            await asyncio.sleep(interval)
            await message.extend_visibility()

    async def stream_info(self, name: str) -> Any:
        del name
        total = 0
        for queue_url in set(self._queue_urls.values()):
            result = await asyncio.to_thread(
                self._client.get_queue_attributes,
                QueueUrl=queue_url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )
            total += int(result.get("Attributes", {}).get("ApproximateNumberOfMessages", 0))
        return SimpleNamespace(
            state=SimpleNamespace(messages=total, consumer_count=len(self._queue_urls))
        )

    async def consumer_info(self, stream: str, consumer: str) -> Any:
        del stream
        matches = [
            url for key, url in self._queue_urls.items() if key.endswith("|" + consumer)
        ]
        if not matches:
            raise EventBusError(f"No queue is mapped for consumer {consumer}")
        result = await asyncio.to_thread(
            self._client.get_queue_attributes,
            QueueUrl=matches[0],
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )
        attributes = result.get("Attributes", {})
        return SimpleNamespace(
            num_pending=int(attributes.get("ApproximateNumberOfMessages", 0)),
            num_ack_pending=int(
                attributes.get("ApproximateNumberOfMessagesNotVisible", 0)
            ),
            num_redelivered=0,
        )

    async def drain(self) -> None:
        for subscription in list(self._subscriptions):
            if not subscription.stop.is_set():
                await subscription.unsubscribe()
        close = getattr(self._client, "close", None)
        if callable(close):
            await asyncio.to_thread(close)


async def connect_event_bus(
    *,
    provider: str | None = None,
    nats_url: str | None = None,
    timeout_seconds: float = 30.0,
) -> EventBus:
    resolved = (provider or os.getenv("EVENT_BUS_PROVIDER", "nats")).strip().lower()
    if resolved == "nats":
        return await NatsEventBus.connect(
            nats_url or os.getenv("NATS_URL", "nats://127.0.0.1:4222"),
            timeout_seconds=timeout_seconds,
        )
    if resolved == "sqs":
        bus = await SqsEventBus.connect()
        await bus.ensure()
        return bus
    raise EventBusError("EVENT_BUS_PROVIDER must be nats or sqs")
