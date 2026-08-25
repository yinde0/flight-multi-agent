from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def parse_timestamp(value: str) -> datetime:
    """Parse an RFC 3339 timestamp and normalize it to UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class VirtualClock:
    """A monotonic clock controlled entirely by a replay scenario."""

    current: datetime | None = None

    def advance_to(self, timestamp: str) -> datetime:
        target = parse_timestamp(timestamp)
        if self.current is not None and target < self.current:
            raise ValueError(
                f"Virtual clock cannot move backwards: {format_timestamp(self.current)} -> {timestamp}"
            )
        self.current = target
        return target

    def now(self) -> datetime:
        if self.current is None:
            raise RuntimeError("Virtual clock has not been initialized")
        return self.current
