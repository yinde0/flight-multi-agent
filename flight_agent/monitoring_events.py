from __future__ import annotations

import asyncio
from typing import Any, Protocol

from flight_agent.event_bus import NatsEventBus, connect_event_bus
from flight_agent.event_delivery import (
    candidate_outbox,
    ensure_event_stream,
    publish_durable_event,
)


class CandidatePublisher(Protocol):
    def publish_candidate(self, candidate: dict[str, Any]) -> None: ...


class NatsCandidatePublisher:
    def __init__(self, nats_url: str) -> None:
        self._nats_url = nats_url

    def publish_candidate(self, candidate: dict[str, Any]) -> None:
        asyncio.run(self.publish_record(candidate_outbox(candidate)))

    async def publish_record(self, record: dict[str, Any]) -> None:
        connection = await NatsEventBus.connect(
            self._nats_url, timeout_seconds=3
        )
        try:
            jetstream = connection.jetstream()
            await ensure_event_stream(jetstream)
            await publish_durable_event(jetstream, record)
        finally:
            await connection.drain()


class ConfiguredEventBusCandidatePublisher:
    """Monitoring publisher selected by EVENT_BUS_PROVIDER."""

    def publish_candidate(self, candidate: dict[str, Any]) -> None:
        asyncio.run(self.publish_record(candidate_outbox(candidate)))

    async def publish_record(self, record: dict[str, Any]) -> None:
        connection = await connect_event_bus(timeout_seconds=3)
        try:
            bus = connection.jetstream()
            await ensure_event_stream(bus)
            await publish_durable_event(bus, record)
        finally:
            await connection.drain()
