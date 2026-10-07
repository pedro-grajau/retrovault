"""Neutral conversation-session events owned by the Concierge module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

SessionChannel = Literal["telegram", "simulator"]
ClaimStatus = Literal["claimed", "duplicate", "in_progress", "reconciliation"]


@dataclass(frozen=True)
class IncomingMessage:
    channel: SessionChannel
    external_user_id: str
    external_chat_id: str
    update_id: int
    message_id: int
    text: str
    sent_at: datetime
    received_at: datetime
    correlation_id: UUID
    context_reference: str | None = None


@dataclass(frozen=True)
class MessageClaim:
    status: ClaimStatus
    session_id: UUID | None = None
    context_game_id: UUID | None = None
    created_session: bool = False
    reply_text: str | None = None
    context_reference_replayed: bool = False
    processing_lease_token: UUID | None = None


@dataclass(frozen=True)
class OutboxReply:
    id: UUID
    channel: SessionChannel
    update_id: int
    chat_id: str
    text: str
    lease_token: UUID


@dataclass(frozen=True)
class ProcessingResult:
    status: ClaimStatus
    session_id: UUID | None = None
    reply_text: str | None = None
