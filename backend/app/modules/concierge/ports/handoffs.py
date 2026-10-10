"""Port for durable human handoff requests and notification delivery."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.concierge.domain.handoff import HandoffNotification


class HandoffStore(Protocol):
    def request_handoff(
        self,
        *,
        session_id: UUID,
        channel: str,
        update_id: int,
        correlation_id: UUID,
        target_chat_id: str,
        notification_text: str,
        context_snapshot: dict[str, object] | None = None,
        now: datetime,
    ) -> bool: ...

    def claim_pending_handoffs(
        self, *, now: datetime, limit: int = 20, lease_seconds: int = 30
    ) -> list[HandoffNotification]: ...

    def mark_handoff_delivered(
        self, request_id: UUID, *, lease_token: UUID
    ) -> bool: ...

    def release_handoff(
        self,
        request_id: UUID,
        *,
        lease_token: UUID,
        retry_delay_seconds: int = 30,
    ) -> bool: ...
