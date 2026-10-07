"""PostgreSQL handoff request and leased notification outbox."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError

from app.modules.concierge.domain.handoff import HandoffNotification, HandoffUnavailable
from app.modules.concierge.ports.handoffs import HandoffStore


class PostgresHandoffRepository(HandoffStore):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def request_handoff(
        self,
        *,
        session_id: UUID,
        channel: str,
        update_id: int,
        correlation_id: UUID,
        target_chat_id: str,
        notification_text: str,
        now: datetime,
    ) -> bool:
        if not target_chat_id.isascii() or not target_chat_id.isdigit():
            raise ValueError("invalid_handoff_recipient")
        try:
            with self.engine.begin() as connection:
                request_id = uuid4()
                inserted = connection.execute(
                    text("""
                        INSERT INTO concierge.handoff_requests (
                            id, session_id, channel, external_update_id,
                            correlation_id, status, requested_at
                        ) VALUES (
                            :id, :session_id, :channel, :update_id,
                            :correlation_id, 'requested', :now
                        ) ON CONFLICT (channel, external_update_id) DO NOTHING
                        RETURNING id
                    """),
                    {
                        "id": request_id,
                        "session_id": session_id,
                        "channel": channel,
                        "update_id": update_id,
                        "correlation_id": correlation_id,
                        "now": now,
                    },
                ).scalar_one_or_none()
                if inserted is None:
                    return True
                connection.execute(
                    text("""
                        INSERT INTO concierge.handoff_outbox (
                            id, request_id, target_chat_id, text, status,
                            created_at
                        ) VALUES (
                            :id, :request_id, :target_chat_id, :text,
                            'pending', :now
                        )
                    """),
                    {
                        "id": uuid4(),
                        "request_id": request_id,
                        "target_chat_id": target_chat_id,
                        "text": notification_text,
                        "now": now,
                    },
                )
                return True
        except SQLAlchemyError as exc:
            raise HandoffUnavailable("concierge_handoff_storage_unavailable") from exc

    def claim_pending_handoffs(
        self, *, now: datetime, limit: int = 20, lease_seconds: int = 30
    ) -> list[HandoffNotification]:
        if limit < 1:
            return []
        token = uuid4()
        try:
            with self.engine.begin() as connection:
                rows = connection.execute(
                    text("""
                        WITH ready AS (
                            SELECT id FROM concierge.handoff_outbox
                            WHERE status = 'pending'
                               OR (status = 'delivering' AND lease_until <= :now)
                            ORDER BY created_at, id
                            LIMIT :limit FOR UPDATE SKIP LOCKED
                        )
                        UPDATE concierge.handoff_outbox AS outbox
                        SET status = 'delivering', lease_token = :token,
                            lease_until = :lease_until
                        FROM ready
                        WHERE outbox.id = ready.id
                        RETURNING outbox.id, outbox.request_id,
                                  outbox.target_chat_id, outbox.text,
                                  outbox.lease_token
                    """),
                    {
                        "now": now,
                        "limit": limit,
                        "token": token,
                        "lease_until": now + timedelta(seconds=lease_seconds),
                    },
                ).mappings().all()
                return [self._notification(row) for row in rows]
        except SQLAlchemyError as exc:
            raise HandoffUnavailable("concierge_handoff_storage_unavailable") from exc

    @staticmethod
    def _notification(row: RowMapping) -> HandoffNotification:
        return HandoffNotification(
            id=row["id"],
            request_id=row["request_id"],
            target_chat_id=row["target_chat_id"],
            text=row["text"],
            lease_token=row["lease_token"],
        )

    def mark_handoff_delivered(
        self, request_id: UUID, *, lease_token: UUID
    ) -> bool:
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    text("""
                        UPDATE concierge.handoff_outbox
                        SET status = 'delivered', lease_token = NULL,
                            lease_until = NULL, delivered_at = now()
                        WHERE request_id = :request_id AND status = 'delivering'
                          AND lease_token = :lease_token
                    """),
                    {"request_id": request_id, "lease_token": lease_token},
                )
                return (result.rowcount or 0) == 1
        except SQLAlchemyError as exc:
            raise HandoffUnavailable("concierge_handoff_storage_unavailable") from exc

    def release_handoff(
        self,
        request_id: UUID,
        *,
        lease_token: UUID,
        retry_delay_seconds: int = 30,
    ) -> bool:
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    text("""
                        UPDATE concierge.handoff_outbox
                        SET lease_until = now() + (:retry_seconds * INTERVAL '1 second')
                        WHERE request_id = :request_id AND status = 'delivering'
                          AND lease_token = :lease_token
                    """),
                    {
                        "request_id": request_id,
                        "lease_token": lease_token,
                        "retry_seconds": retry_delay_seconds,
                    },
                )
                return (result.rowcount or 0) == 1
        except SQLAlchemyError as exc:
            raise HandoffUnavailable("concierge_handoff_storage_unavailable") from exc
