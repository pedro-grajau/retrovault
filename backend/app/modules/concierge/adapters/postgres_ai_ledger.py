"""Transactional PostgreSQL AI budget ledger."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.modules.concierge.domain.ai_ledger import (
    AiLedgerUnavailable,
    ReservationGrant,
)
from app.modules.concierge.ports.ai_ledger import AiLedger


class PostgresAiLedger(AiLedger):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def reserve(
        self,
        *,
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
    ) -> ReservationGrant:
        if (
            reserved_cost_usd <= 0
            or monthly_budget_usd <= 0
            or monthly_budget_usd > Decimal("25.00")
            or input_token_limit < 1
            or output_token_limit < 1
        ):
            raise ValueError("invalid_ai_reservation")
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text("""
                        INSERT INTO concierge.ai_ledger_months (
                            period_start, posted_cost_usd, reserved_cost_usd,
                            created_at, updated_at
                        ) VALUES (
                            :period_start, 0, 0, :now, :now
                        ) ON CONFLICT (period_start) DO NOTHING
                    """),
                    {"period_start": period_start, "now": now},
                )
                connection.execute(
                    text("""
                        SELECT posted_cost_usd, reserved_cost_usd
                        FROM concierge.ai_ledger_months
                        WHERE period_start = :period_start
                        FOR UPDATE
                    """),
                    {"period_start": period_start},
                ).mappings().one()
                self._expire_for_period(connection, period_start, now)
                existing = connection.execute(
                    text("""
                        SELECT id, status
                        FROM concierge.ai_ledger_reservations
                        WHERE channel = :channel AND external_update_id = :update_id
                        FOR UPDATE
                    """),
                    {"channel": channel, "update_id": update_id},
                ).mappings().first()
                if existing is not None:
                    return ReservationGrant(
                        existing["id"],
                        existing["status"],
                        created=False,
                        allowed=False,
                    )

                totals = connection.execute(
                    text("""
                        SELECT posted_cost_usd, reserved_cost_usd
                        FROM concierge.ai_ledger_months
                        WHERE period_start = :period_start
                    """),
                    {"period_start": period_start},
                ).mappings().one()
                if (
                    Decimal(totals["posted_cost_usd"])
                    + Decimal(totals["reserved_cost_usd"])
                    + reserved_cost_usd
                    > monthly_budget_usd
                ):
                    return ReservationGrant(
                        uuid4(), "released", created=False, allowed=False
                    )

                reservation_id = uuid4()
                connection.execute(
                    text("""
                        INSERT INTO concierge.ai_ledger_reservations (
                            id, session_id, channel, external_update_id,
                            period_start, correlation_id, model_snapshot,
                            prompt_version, workflow_version, configuration_version,
                            input_token_limit, output_token_limit,
                            reserved_cost_usd, status, created_at, updated_at,
                            expires_at
                        ) VALUES (
                            :id, :session_id, :channel, :update_id,
                            :period_start, :correlation_id, :model_snapshot,
                            :prompt_version, :workflow_version, :configuration_version,
                            :input_token_limit, :output_token_limit,
                            :reserved_cost_usd, 'reserved', :now, :now,
                            :expires_at
                        )
                    """),
                    {
                        "id": reservation_id,
                        "session_id": session_id,
                        "channel": channel,
                        "update_id": update_id,
                        "period_start": period_start,
                        "correlation_id": correlation_id,
                        "model_snapshot": model_snapshot,
                        "prompt_version": prompt_version,
                        "workflow_version": workflow_version,
                        "configuration_version": configuration_version,
                        "input_token_limit": input_token_limit,
                        "output_token_limit": output_token_limit,
                        "reserved_cost_usd": reserved_cost_usd,
                        "now": now,
                        "expires_at": expires_at,
                    },
                )
                connection.execute(
                    text("""
                        UPDATE concierge.ai_ledger_months
                        SET reserved_cost_usd = reserved_cost_usd + :amount,
                            updated_at = :now
                        WHERE period_start = :period_start
                    """),
                    {
                        "amount": reserved_cost_usd,
                        "now": now,
                        "period_start": period_start,
                    },
                )
                return ReservationGrant(
                    reservation_id, "reserved", created=True, allowed=True
                )
        except SQLAlchemyError as exc:
            raise AiLedgerUnavailable("concierge_ai_ledger_unavailable") from exc

    @staticmethod
    def _expire_for_period(connection: Any, period_start: date, now: datetime) -> None:
        rows = connection.execute(
            text("""
                SELECT id, reserved_cost_usd
                FROM concierge.ai_ledger_reservations
                WHERE period_start = :period_start
                  AND status = 'reserved' AND expires_at <= :now
                ORDER BY expires_at, id
                FOR UPDATE
            """),
            {"period_start": period_start, "now": now},
        ).mappings().all()
        for row in rows:
            # An ambiguous call is conservatively posted at its full reservation.
            connection.execute(
                text("""
                    UPDATE concierge.ai_ledger_reservations
                    SET status = 'expired', updated_at = :now
                    WHERE id = :id AND status = 'reserved'
                """),
                {"id": row["id"], "now": now},
            )
            connection.execute(
                text("""
                    UPDATE concierge.ai_ledger_months
                    SET posted_cost_usd = posted_cost_usd + :amount,
                        reserved_cost_usd = reserved_cost_usd - :amount,
                        updated_at = :now
                    WHERE period_start = :period_start
                """),
                {
                    "amount": row["reserved_cost_usd"],
                    "now": now,
                    "period_start": period_start,
                },
            )

    def reconcile(
        self,
        reservation_id: UUID,
        *,
        input_tokens: int,
        output_tokens: int,
        actual_cost_usd: Decimal,
        now: datetime,
    ) -> bool:
        if input_tokens < 0 or output_tokens < 0 or actual_cost_usd < 0:
            return False
        try:
            with self.engine.begin() as connection:
                period_start = connection.execute(
                    text("""
                        SELECT period_start FROM concierge.ai_ledger_reservations
                        WHERE id = :id
                    """),
                    {"id": reservation_id},
                ).scalar_one_or_none()
                if period_start is None:
                    return False
                connection.execute(
                    text("""
                        SELECT period_start FROM concierge.ai_ledger_months
                        WHERE period_start = :period_start FOR UPDATE
                    """),
                    {"period_start": period_start},
                ).scalar_one()
                row = connection.execute(
                    text("""
                        SELECT status, reserved_cost_usd,
                               actual_input_tokens, actual_output_tokens,
                               actual_cost_usd
                        FROM concierge.ai_ledger_reservations
                        WHERE id = :id FOR UPDATE
                    """),
                    {"id": reservation_id},
                ).mappings().one()
                if row["status"] == "posted":
                    return (
                        row["actual_input_tokens"] == input_tokens
                        and row["actual_output_tokens"] == output_tokens
                        and Decimal(row["actual_cost_usd"]) == actual_cost_usd
                    )
                if row["status"] not in {"reserved", "expired"}:
                    return False
                prior_status = row["status"]
                reserved = Decimal(row["reserved_cost_usd"])
                connection.execute(
                    text("""
                        UPDATE concierge.ai_ledger_reservations
                        SET status = 'posted', actual_input_tokens = :input_tokens,
                            actual_output_tokens = :output_tokens,
                            actual_cost_usd = :actual_cost_usd, updated_at = :now
                        WHERE id = :id
                    """),
                    {
                        "id": reservation_id,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "actual_cost_usd": actual_cost_usd,
                        "now": now,
                    },
                )
                if prior_status == "reserved":
                    connection.execute(
                        text("""
                            UPDATE concierge.ai_ledger_months
                            SET posted_cost_usd = posted_cost_usd + :actual,
                                reserved_cost_usd = reserved_cost_usd - :reserved,
                                updated_at = :now
                            WHERE period_start = :period_start
                        """),
                        {
                            "actual": actual_cost_usd,
                            "reserved": reserved,
                            "now": now,
                            "period_start": period_start,
                        },
                    )
                else:
                    # Late usage replaces the conservative amount posted at expiry.
                    connection.execute(
                        text("""
                            UPDATE concierge.ai_ledger_months
                            SET posted_cost_usd = posted_cost_usd + :delta,
                                updated_at = :now
                            WHERE period_start = :period_start
                        """),
                        {
                            "delta": actual_cost_usd - reserved,
                            "now": now,
                            "period_start": period_start,
                        },
                    )
                return True
        except SQLAlchemyError as exc:
            raise AiLedgerUnavailable("concierge_ai_ledger_unavailable") from exc

    def release(self, reservation_id: UUID, *, now: datetime) -> bool:
        try:
            with self.engine.begin() as connection:
                period_start = connection.execute(
                    text("""
                        SELECT period_start FROM concierge.ai_ledger_reservations
                        WHERE id = :id
                    """),
                    {"id": reservation_id},
                ).scalar_one_or_none()
                if period_start is None:
                    return False
                connection.execute(
                    text("""
                        SELECT period_start FROM concierge.ai_ledger_months
                        WHERE period_start = :period_start FOR UPDATE
                    """),
                    {"period_start": period_start},
                ).scalar_one()
                row = connection.execute(
                    text("""
                        SELECT status, reserved_cost_usd
                        FROM concierge.ai_ledger_reservations
                        WHERE id = :id FOR UPDATE
                    """),
                    {"id": reservation_id},
                ).mappings().one()
                if row["status"] == "released":
                    return True
                if row["status"] != "reserved":
                    return False
                connection.execute(
                    text("""
                        UPDATE concierge.ai_ledger_reservations
                        SET status = 'released', updated_at = :now
                        WHERE id = :id
                    """),
                    {"id": reservation_id, "now": now},
                )
                connection.execute(
                    text("""
                        UPDATE concierge.ai_ledger_months
                        SET reserved_cost_usd = reserved_cost_usd - :amount,
                            updated_at = :now
                        WHERE period_start = :period_start
                    """),
                    {
                        "amount": row["reserved_cost_usd"],
                        "now": now,
                        "period_start": period_start,
                    },
                )
                return True
        except SQLAlchemyError as exc:
            raise AiLedgerUnavailable("concierge_ai_ledger_unavailable") from exc

    def expire_orphans(self, *, now: datetime, limit: int = 100) -> int:
        if limit < 1:
            return 0
        try:
            with self.engine.begin() as connection:
                periods = connection.execute(
                    text("""
                        SELECT DISTINCT period_start
                        FROM concierge.ai_ledger_reservations
                        WHERE status = 'reserved' AND expires_at <= :now
                        ORDER BY period_start
                        LIMIT :limit
                    """),
                    {"now": now, "limit": limit},
                ).scalars().all()
                if not periods:
                    return 0
                connection.execute(
                    text("""
                        SELECT period_start FROM concierge.ai_ledger_months
                        WHERE period_start = ANY(:periods)
                        ORDER BY period_start FOR UPDATE
                    """),
                    {"periods": list(periods)},
                ).all()
                rows = connection.execute(
                    text("""
                        SELECT id, period_start, reserved_cost_usd
                        FROM concierge.ai_ledger_reservations
                        WHERE status = 'reserved' AND expires_at <= :now
                          AND period_start = ANY(:periods)
                        ORDER BY period_start, expires_at, id
                        LIMIT :limit FOR UPDATE SKIP LOCKED
                    """),
                    {"now": now, "periods": list(periods), "limit": limit},
                ).mappings().all()
                for row in rows:
                    self._expire_one(connection, row, now)
                return len(rows)
        except SQLAlchemyError as exc:
            raise AiLedgerUnavailable("concierge_ai_ledger_unavailable") from exc

    @staticmethod
    def _expire_one(connection: Any, row: Any, now: datetime) -> None:
        connection.execute(
            text("""
                UPDATE concierge.ai_ledger_reservations
                SET status = 'expired', updated_at = :now
                WHERE id = :id AND status = 'reserved'
            """),
            {"id": row["id"], "now": now},
        )
        connection.execute(
            text("""
                UPDATE concierge.ai_ledger_months
                SET posted_cost_usd = posted_cost_usd + :amount,
                    reserved_cost_usd = reserved_cost_usd - :amount,
                    updated_at = :now
                WHERE period_start = :period_start
            """),
            {
                "amount": row["reserved_cost_usd"],
                "period_start": row["period_start"],
                "now": now,
            },
        )

    def purge_expired_metadata(self, *, before: datetime) -> int:
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    text("""
                        DELETE FROM concierge.ai_ledger_reservations
                        WHERE created_at < :before
                          AND status IN ('posted', 'released', 'expired')
                    """),
                    {"before": before},
                )
                return result.rowcount or 0
        except SQLAlchemyError as exc:
            raise AiLedgerUnavailable("concierge_ai_ledger_unavailable") from exc
