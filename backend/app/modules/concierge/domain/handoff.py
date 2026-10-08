"""Idempotent Sandbox handoff request and notification values."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


class HandoffUnavailable(RuntimeError):
    """The durable handoff store is unavailable."""


@dataclass(frozen=True)
class HandoffNotification:
    id: UUID
    request_id: UUID
    target_chat_id: str
    text: str
    lease_token: UUID
