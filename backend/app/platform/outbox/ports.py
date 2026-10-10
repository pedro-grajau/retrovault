"""Consumer-specific event delivery; the producer outbox remains authoritative."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class PublicEvent:
    id: UUID
    topic: str
    aggregate_id: UUID
    payload: dict[str, object]
    lease_token: UUID
    attempts: int
    occurred_at: datetime


class EventReader(Protocol):
    def claim(self, *, consumer: str, limit: int = 20) -> list[PublicEvent]: ...
    def finish(
        self,
        event: PublicEvent,
        *,
        consumer: str,
        retry: bool = False,
        incompatible: bool = False,
    ) -> None: ...
