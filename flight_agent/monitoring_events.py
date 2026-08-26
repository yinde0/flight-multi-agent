from __future__ import annotations

import asyncio
import json

from typing import Any, Protocol

import nats


DISRUPTION_CANDIDATE_SUBJECT = "travel.disruption_candidate.v1"
DISRUPTION_CONFIRMED_SUBJECT = "travel.disruption_confirmed.v1"


class CandidatePublisher(Protocol):
    def publish_candidate(self, candidate: dict[str, Any]) -> None: ...


class NatsCandidatePublisher:
    def __init__(self, nats_url: str) -> None:
        self._nats_url = nats_url

    def publish_candidate(self, candidate: dict[str, Any]) -> None:
        asyncio.run(self._publish(candidate))

    async def _publish(self, candidate: dict[str, Any]) -> None:
        connection = await nats.connect(
            servers=[self._nats_url],
            connect_timeout=3,
            max_reconnect_attempts=3,
        )
        try:
            await connection.publish(
                DISRUPTION_CANDIDATE_SUBJECT,
                json.dumps(candidate, separators=(",", ":")).encode("utf-8"),
            )
            await connection.flush(timeout=3)
        finally:
            await connection.drain()
