"""Ports for durable session processing and channel delivery."""

from __future__ import annotations

from collections.abc import ContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.concierge.domain.session import (
    IncomingMessage,
    MessageClaim,
    OutboxReply,
)


class SessionStore(Protocol):
    def session_processing_lock(self, session_id: UUID) -> ContextManager[None]: ...

    def is_processing_lease_current(
        self,
        update_id: int,
        *,
        channel: str,
        session_id: UUID,
        processing_lease_token: UUID,
        now: datetime,
    ) -> bool: ...

    def claim_message(
        self,
        message: IncomingMessage,
        *,
        safe_text: str,
        context_game_id: UUID | None,
        now: datetime,
        max_age_seconds: int,
        context_reference_hash: str | None = None,
    ) -> MessageClaim: ...

    def complete_message(
        self,
        update_id: int,
        *,
        channel: str,
        session_id: UUID,
        processing_lease_token: UUID,
        reply_text: str,
        workflow_version: str,
    ) -> None: ...

    def claim_reply(
        self,
        update_id: int,
        *,
        channel: str,
        now: datetime,
        lease_seconds: int = 30,
    ) -> OutboxReply | None: ...

    def claim_pending_replies(
        self,
        *,
        now: datetime,
        limit: int = 20,
        lease_seconds: int = 30,
    ) -> list[OutboxReply]: ...

    def mark_reply_delivered(
        self, update_id: int, *, channel: str, lease_token: UUID
    ) -> bool: ...

    def release_reply(
        self,
        update_id: int,
        *,
        channel: str,
        lease_token: UUID,
        retry_delay_seconds: int = 30,
    ) -> bool: ...

    def purge_expired_sessions(self, retention_days: int) -> int: ...


class TelegramMessenger(Protocol):
    async def send_chat_action(self, chat_id: str, action: str = "typing") -> None: ...

    async def send_message(self, chat_id: str, text: str) -> None: ...
