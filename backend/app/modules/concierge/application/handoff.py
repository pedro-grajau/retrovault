"""Sandbox handoff policy with a single, explicitly configured recipient."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from app.modules.concierge.domain.handoff import HandoffUnavailable
from app.modules.concierge.ports.handoffs import HandoffStore

HandoffStatus = Literal["registered", "disabled", "unavailable"]


@dataclass(frozen=True)
class HandoffDecision:
    status: HandoffStatus


class HandoffService:
    def __init__(
        self,
        store: HandoffStore | None,
        allowed_user_ids: frozenset[int],
        *,
        telegram_token_configured: bool = True,
    ) -> None:
        self.store = store
        self.allowed_user_ids = allowed_user_ids
        self.telegram_token_configured = telegram_token_configured

    def request(
        self,
        *,
        session_id: UUID,
        channel: str,
        update_id: int,
        correlation_id: UUID,
        now: datetime | None = None,
    ) -> HandoffDecision:
        if (
            len(self.allowed_user_ids) != 1
            or self.store is None
            or not self.telegram_token_configured
            or next(iter(self.allowed_user_ids)) <= 0
        ):
            return HandoffDecision("disabled")
        recipient_id = next(iter(self.allowed_user_ids))
        notification = (
            "Sandbox: solicitação de revisão humana registrada para a sessão "
            f"{session_id}. Correlação: {correlation_id}. "
            "Nenhum histórico de conversa foi encaminhado."
        )
        try:
            created_or_existing = self.store.request_handoff(
                session_id=session_id,
                channel=channel,
                update_id=update_id,
                correlation_id=correlation_id,
                target_chat_id=str(recipient_id),
                notification_text=notification,
                now=now or datetime.now(UTC),
            )
        except HandoffUnavailable:
            return HandoffDecision("unavailable")
        return HandoffDecision("registered" if created_or_existing else "unavailable")
