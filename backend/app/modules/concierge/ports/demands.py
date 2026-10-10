"""Public persistence boundary for consent and durable notification work."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.concierge.domain.demand import Demand, DemandNotification


class DemandStore(Protocol):
    def propose(
        self,
        *,
        owner_key: str,
        channel: str,
        chat_id: str,
        session_id: UUID,
        update_id: int,
        correlation_id: UUID,
        title: str,
        platform: str,
        mode: str,
        game_id: UUID | None,
        context: dict[str, object],
    ) -> Demand: ...
    def confirm(
        self, demand_id: UUID, *, owner_key: str, channel: str, update_id: int
    ) -> Demand | None: ...
    def cancel(self, demand_id: UUID, *, owner_key: str, channel: str) -> bool: ...
    def get_owned(
        self, demand_id: UUID, *, owner_key: str, channel: str
    ) -> Demand | None: ...
    def list_owned(self, *, owner_key: str, channel: str) -> list[Demand]: ...
    def active(self) -> list[Demand]: ...
    def enqueue(self, demand_id: UUID, *, event_id: UUID, game_id: UUID) -> None: ...
    def close_sold(self, demand_id: UUID, *, event_id: UUID) -> None: ...
    def claim(
        self, *, now: datetime, limit: int = 20, channel: str | None = None
    ) -> list[DemandNotification]: ...
    def complete(self, notification: DemandNotification) -> bool: ...
    def retry(
        self, notification: DemandNotification, *, suppressed: bool = False
    ) -> None: ...
