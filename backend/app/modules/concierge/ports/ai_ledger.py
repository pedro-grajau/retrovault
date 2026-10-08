"""Port for the durable AI cost ledger."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from app.modules.concierge.domain.ai_ledger import AiOperation, ReservationGrant


class AiLedger(Protocol):
    def reserve(
        self,
        *,
        operation: AiOperation = "intent_extraction",
        session_id: UUID,
        channel: str,
        update_id: int,
        correlation_id: UUID,
        period_start: date,
        model_snapshot: str,
        prompt_version: str,
        workflow_version: str,
        configuration_version: str,
        input_token_limit: int,
        output_token_limit: int,
        reserved_cost_usd: Decimal,
        monthly_budget_usd: Decimal,
        now: datetime,
        expires_at: datetime,
    ) -> ReservationGrant: ...

    def reconcile(
        self,
        reservation_id: UUID,
        *,
        input_tokens: int,
        output_tokens: int,
        actual_cost_usd: Decimal,
        now: datetime,
    ) -> bool: ...

    def release(self, reservation_id: UUID, *, now: datetime) -> bool: ...

    def expire_orphans(self, *, now: datetime, limit: int = 100) -> int: ...

    def purge_expired_metadata(self, *, before: datetime) -> int: ...
