from __future__ import annotations

import asyncio
import json

from flight_agent.event_bus import SqsEventBus, SqsMessage


class FakeSqs:
    def __init__(self) -> None:
        self.sent = []
        self.deleted = []
        self.visibility = []
        self.receives = []
        self.messages = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return {"MessageId": f"message-{len(self.sent)}"}

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs)

    def change_message_visibility(self, **kwargs):
        self.visibility.append(kwargs)

    def receive_message(self, **kwargs):
        self.receives.append(kwargs)
        messages, self.messages = self.messages, []
        return {"Messages": messages}

    def get_queue_attributes(self, **kwargs):
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": "0",
                "ApproximateNumberOfMessagesNotVisible": "0",
            }
        }

    def close(self):
        return None


class MemoryClaims:
    def __init__(self) -> None:
        self.states = {}

    def claim(self, consumer, event_id):
        key = (consumer, event_id)
        if key not in self.states:
            self.states[key] = "processing"
            return "claimed"
        return "complete" if self.states[key] == "complete" else "busy"

    def complete(self, consumer, event_id):
        self.states[(consumer, event_id)] = "complete"

    def release(self, consumer, event_id):
        self.states.pop((consumer, event_id), None)


def queues():
    return {
        "travel.disruption_candidate.v1|travel-eval-agent-v1": "candidate.fifo",
        "travel.disruption_confirmed.v1|travel-notification-action-v1": "notification",
        "travel.disruption_confirmed.v1|travel-flight-search-action-v1": "search",
    }


def envelope(event_id="decision-unit"):
    return json.dumps(
        {
            "schema_version": "1.0.0",
            "event_id": event_id,
            "event_type": "disruption_confirmed",
            "occurred_at": "2026-09-15T06:00:00Z",
            "payload": {"trip_id": "trip-unit"},
        }
    ).encode()


def test_sqs_publish_fans_out_and_uses_event_id_for_fifo_deduplication():
    client = FakeSqs()
    bus = SqsEventBus(client=client, queue_urls=queues(), idempotency=MemoryClaims())

    result = asyncio.run(
        bus.publish(
            "travel.disruption_confirmed.v1",
            envelope(),
            message_id="decision-unit",
            headers={"traceparent": "00-unit"},
        )
    )

    assert result["destinations"] == 2
    assert {call["QueueUrl"] for call in client.sent} == {"notification", "search"}
    assert all(call["MessageAttributes"]["event_id"]["StringValue"] == "decision-unit" for call in client.sent)


def test_sqs_fifo_publish_sets_native_deduplication_fields():
    client = FakeSqs()
    bus = SqsEventBus(client=client, queue_urls=queues(), idempotency=MemoryClaims())

    asyncio.run(
        bus.publish("travel.disruption_candidate.v1", envelope("cand-unit"))
    )

    call = client.sent[0]
    assert call["MessageDeduplicationId"] == "cand-unit"
    assert call["MessageGroupId"] == "trip-unit"


def test_sqs_redrive_targets_only_the_failed_consumer_queue():
    client = FakeSqs()
    bus = SqsEventBus(client=client, queue_urls=queues(), idempotency=MemoryClaims())

    asyncio.run(
        bus.publish(
            "travel.disruption_confirmed.v1",
            envelope(),
            target_consumer="travel-notification-action-v1",
        )
    )

    assert [call["QueueUrl"] for call in client.sent] == ["notification"]


def test_sqs_message_ack_visibility_retry_and_dead_letter():
    client = FakeSqs()
    claims = MemoryClaims()
    bus = SqsEventBus(
        client=client,
        queue_urls=queues(),
        dead_letter_urls={"travel-eval-agent-v1": "eval-dlq"},
        idempotency=claims,
    )
    claims.claim("travel-eval-agent-v1", "cand-unit")
    raw = {
        "Body": envelope("cand-unit").decode(),
        "ReceiptHandle": "receipt-1",
        "Attributes": {"ApproximateReceiveCount": "3"},
        "MessageAttributes": {},
    }
    message = SqsMessage(
        bus=bus,
        queue_url="candidate.fifo",
        consumer="travel-eval-agent-v1",
        raw=raw,
        event_id="cand-unit",
    )

    asyncio.run(message.extend_visibility(45))
    asyncio.run(message.term())

    assert client.visibility[0]["VisibilityTimeout"] == 45
    assert client.sent[0]["QueueUrl"] == "eval-dlq"
    assert client.deleted[0]["ReceiptHandle"] == "receipt-1"
    assert ("travel-eval-agent-v1", "cand-unit") not in claims.states


def test_sqs_subscription_uses_long_poll_and_acknowledges_once():
    async def scenario():
        client = FakeSqs()
        claims = MemoryClaims()
        bus = SqsEventBus(client=client, queue_urls=queues(), idempotency=claims)
        client.messages = [
            {
                "Body": envelope("cand-unit").decode(),
                "ReceiptHandle": "receipt-2",
                "Attributes": {"ApproximateReceiveCount": "1"},
                "MessageAttributes": {},
            }
        ]
        handled = asyncio.Event()

        async def callback(message):
            await message.ack_sync()
            handled.set()

        subscription = await bus.subscribe(
            "travel.disruption_candidate.v1",
            durable_name="travel-eval-agent-v1",
            callback=callback,
        )
        await asyncio.wait_for(handled.wait(), timeout=1)
        await subscription.unsubscribe()
        assert client.receives[0]["WaitTimeSeconds"] == 20
        assert client.receives[0]["VisibilityTimeout"] == 60
        assert len(client.deleted) == 1

    asyncio.run(scenario())
