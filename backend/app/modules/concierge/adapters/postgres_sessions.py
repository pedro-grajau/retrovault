"""PostgreSQL storage and LangGraph checkpoint factory for Concierge sessions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from sqlalchemy import Connection, Engine, make_url, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError

from app.modules.concierge.domain.session import (
    IncomingMessage,
    MessageClaim,
    OutboxReply,
)

WORKFLOW_VERSION = "2.3.v1"


class SessionsUnavailable(RuntimeError):
    """Durable Concierge session storage is unavailable."""


class SessionLeaseLost(RuntimeError):
    """A stale session worker lost its processing lease to a newer worker."""


class PostgresCheckpointFactory:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @contextmanager
    def __call__(self) -> Generator[PostgresSaver]:
        dsn = make_url(self.database_url).set(drivername="postgresql").render_as_string(
            hide_password=False
        )
        try:
            connection = cast(
                psycopg.Connection[dict[str, Any]],
                psycopg.connect(
                    dsn,
                    autocommit=True,
                    options="-c search_path=concierge",
                    row_factory=cast(Any, dict_row),
                ),
            )
            with connection:
                checkpointer = PostgresSaver(
                    connection,
                    serde=JsonPlusSerializer(
                        allowed_json_modules=(), allowed_msgpack_modules=()
                    ),
                )
                yield checkpointer
        except psycopg.Error as exc:
            raise SessionsUnavailable("concierge_checkpoint_unavailable") from exc

    def setup(self) -> None:
        with self() as checkpointer:
            checkpointer.setup()


class PostgresSessionRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def session_processing_lock(
        self, session_id: UUID
    ) -> Generator[None]:
        # A session-level PostgreSQL advisory lock spans graph checkpoint writes
        # and message completion, including across multiple app processes.
        lock_key = int.from_bytes(
            hashlib.sha256(session_id.bytes).digest()[:8], "big", signed=True
        )
        connection = self.engine.connect()
        acquired = False
        try:
            connection.execute(
                text("SELECT pg_advisory_lock(:lock_key)"), {"lock_key": lock_key}
            )
            acquired = True
            yield
        except SQLAlchemyError as exc:
            raise SessionsUnavailable("concierge_session_storage_unavailable") from exc
        finally:
            try:
                if acquired:
                    try:
                        connection.execute(
                            text("SELECT pg_advisory_unlock(:lock_key)"),
                            {"lock_key": lock_key},
                        )
                    except SQLAlchemyError as exc:
                        connection.invalidate()
                        raise SessionsUnavailable(
                            "concierge_session_storage_unavailable"
                        ) from exc
            finally:
                connection.close()

    def is_processing_lease_current(
        self,
        update_id: int,
        *,
        channel: str,
        session_id: UUID,
        processing_lease_token: UUID,
        now: datetime,
    ) -> bool:
        try:
            with self.engine.connect() as connection:
                return bool(
                    connection.execute(
                        text("""
                            SELECT EXISTS (
                                SELECT 1 FROM concierge.messages
                                WHERE channel = :channel
                                  AND external_update_id = :update_id
                                  AND session_id = :session_id
                                  AND status = 'processing'
                                  AND lease_token = :lease_token
                                  AND lease_until > :now
                            )
                        """),
                        {
                            "channel": channel,
                            "update_id": update_id,
                            "session_id": session_id,
                            "lease_token": processing_lease_token,
                            "now": now,
                        },
                    ).scalar_one()
                )
        except SQLAlchemyError as exc:
            raise SessionsUnavailable("concierge_session_storage_unavailable") from exc

    def claim_message(
        self,
        message: IncomingMessage,
        *,
        safe_text: str,
        context_game_id: UUID | None,
        now: datetime,
        max_age_seconds: int,
        context_reference_hash: str | None = None,
    ) -> MessageClaim:
        try:
            with self.engine.begin() as connection:
                existing = connection.execute(
                    text("""
                        SELECT id, session_id, status, lease_until,
                               external_user_id, external_chat_id, external_message_id
                        FROM concierge.messages
                        WHERE channel = :channel AND external_update_id = :update_id
                        FOR UPDATE
                    """),
                    {"channel": message.channel, "update_id": message.update_id},
                ).mappings().first()
                if existing is not None:
                    return self._claim_existing(connection, existing, message, now)

                session_row = None
                if message.sent_at >= now - timedelta(seconds=max_age_seconds):
                    session_row = self._find_or_create_session(
                        connection, message, now
                    )
                    concurrent_duplicate = connection.execute(
                        text("""
                            SELECT id, session_id, status, lease_until,
                                   external_user_id, external_chat_id, external_message_id
                            FROM concierge.messages
                            WHERE channel = :channel AND external_update_id = :update_id
                            FOR UPDATE
                        """),
                        {"channel": message.channel, "update_id": message.update_id},
                    ).mappings().first()
                    if concurrent_duplicate is not None:
                        return self._claim_existing(
                            connection, concurrent_duplicate, message, now
                        )
                    if not session_row["created"] and (
                        message.sent_at < session_row["last_message_at"]
                        or (
                        message.sent_at == session_row["last_message_at"]
                        and message.message_id <= session_row["last_message_id"]
                        )
                    ):
                        self._insert_message(
                            connection,
                            message,
                            safe_text=safe_text,
                            session_id=session_row["id"],
                            status="reconciliation",
                            lease_until=None,
                            lease_token=None,
                        )
                        return MessageClaim("reconciliation", session_row["id"])
                    if not session_row["created"]:
                        processing_message_id = connection.execute(
                            text("""
                                SELECT id FROM concierge.messages
                                WHERE session_id = :session_id
                                  AND status = 'processing'
                                  AND lease_until > :now
                                LIMIT 1
                            """),
                            {"session_id": session_row["id"], "now": now},
                        ).scalar_one_or_none()
                        if processing_message_id is not None:
                            return MessageClaim("in_progress", session_row["id"])
                    context_reference_replayed = False
                    if context_reference_hash is not None:
                        consumed = connection.execute(
                            text("""
                                INSERT INTO concierge.context_reference_uses (
                                    token_hash, session_id, channel,
                                    external_update_id, consumed_at
                                ) VALUES (
                                    :token_hash, :session_id, :channel,
                                    :update_id, :now
                                ) ON CONFLICT (token_hash) DO NOTHING
                                RETURNING token_hash
                            """),
                            {
                                "token_hash": context_reference_hash,
                                "session_id": session_row["id"],
                                "channel": message.channel,
                                "update_id": message.update_id,
                                "now": now,
                            },
                        ).scalar_one_or_none()
                        context_reference_replayed = consumed is None
                        if context_reference_replayed:
                            context_game_id = None
                    processing_lease_token = uuid4()
                    connection.execute(
                        text("""
                            UPDATE concierge.sessions
                            SET last_message_at = :sent_at, last_message_id = :message_id,
                                updated_at = :now,
                                context_game_id = COALESCE(:context_game_id, context_game_id)
                            WHERE id = :session_id
                        """),
                        {
                            "sent_at": message.sent_at,
                            "message_id": message.message_id,
                            "now": now,
                            "context_game_id": context_game_id,
                            "session_id": session_row["id"],
                        },
                    )

                self._insert_message(
                    connection,
                    message,
                    safe_text=safe_text,
                    session_id=session_row["id"] if session_row else None,
                    status=("processing" if session_row else "reconciliation"),
                    lease_until=(now + timedelta(minutes=2) if session_row else None),
                    lease_token=(processing_lease_token if session_row else None),
                )
                if session_row is None:
                    return MessageClaim("reconciliation")
                return MessageClaim(
                    "claimed",
                    session_row["id"],
                    context_game_id or session_row["context_game_id"],
                    session_row["created"],
                    processing_lease_token=processing_lease_token,
                    context_reference_replayed=(
                        context_reference_hash is not None
                        and context_reference_replayed
                    ),
                )
        except SQLAlchemyError as exc:
            raise SessionsUnavailable("concierge_session_storage_unavailable") from exc

    @staticmethod
    def _claim_existing(
        connection: Connection,
        existing: RowMapping,
        message: IncomingMessage,
        now: datetime,
    ) -> MessageClaim:
        if (
            existing["external_user_id"] != message.external_user_id
            or existing["external_chat_id"] != message.external_chat_id
            or existing["external_message_id"] != message.message_id
        ):
            connection.execute(
                text("""
                    INSERT INTO concierge.message_replay_anomalies (
                        id, source_message_id, incoming_external_user_hash,
                        incoming_external_chat_hash, incoming_message_id,
                        received_at, correlation_id
                    ) VALUES (
                        :id, :source_message_id, :user_hash,
                        :chat_hash, :message_id, :received_at, :correlation_id
                    ) ON CONFLICT (
                        source_message_id, incoming_external_user_hash,
                        incoming_external_chat_hash, incoming_message_id
                    ) DO NOTHING
                """),
                {
                    "id": uuid4(),
                    "source_message_id": existing["id"],
                    "user_hash": hashlib.sha256(
                        message.external_user_id.encode("utf-8")
                    ).hexdigest(),
                    "chat_hash": hashlib.sha256(
                        message.external_chat_id.encode("utf-8")
                    ).hexdigest(),
                    "message_id": message.message_id,
                    "received_at": message.received_at,
                    "correlation_id": message.correlation_id,
                },
            )
            return MessageClaim("reconciliation", existing["session_id"])
        status = existing["status"]
        session_id = existing["session_id"]
        if status == "processed":
            return MessageClaim("duplicate", session_id)
        if status == "reconciliation":
            return MessageClaim("reconciliation", session_id)
        if existing["lease_until"] is not None and existing["lease_until"] > now:
            return MessageClaim("in_progress", session_id)
        lease_token = uuid4()
        connection.execute(
            text("""
                UPDATE concierge.messages
                SET status = 'processing', lease_token = :lease_token,
                    lease_until = :lease_until
                WHERE id = :message_id
            """),
            {
                "lease_until": now + timedelta(minutes=2),
                "lease_token": lease_token,
                "message_id": existing["id"],
            },
        )
        game_id = connection.execute(
            text("SELECT context_game_id FROM concierge.sessions WHERE id = :session_id"),
            {"session_id": session_id},
        ).scalar_one_or_none()
        return MessageClaim(
            "claimed", session_id, game_id, False,
            processing_lease_token=lease_token,
        )

    @staticmethod
    def _find_or_create_session(
        connection: Connection,
        message: IncomingMessage,
        now: datetime,
    ) -> dict[str, Any]:
        inserted = connection.execute(
            text("""
                INSERT INTO concierge.sessions (
                    id, channel, external_user_id, external_chat_id,
                    context_game_id, status, workflow_version, correlation_id,
                    started_at, updated_at, last_message_at, last_message_id
                ) VALUES (
                    :id, :channel, :user_id, :chat_id,
                    NULL, 'active', :workflow_version, :correlation_id,
                    :now, :now, :sent_at, :message_id
                )
                ON CONFLICT (channel, external_user_id) WHERE status = 'active'
                DO NOTHING
                RETURNING id, context_game_id, last_message_at, last_message_id
            """),
            {
                "id": uuid4(),
                "channel": message.channel,
                "user_id": message.external_user_id,
                "chat_id": message.external_chat_id,
                "workflow_version": WORKFLOW_VERSION,
                "correlation_id": message.correlation_id,
                "now": now,
                "sent_at": message.sent_at,
                "message_id": message.message_id,
            },
        ).mappings().first()
        if inserted is not None:
            return {
                "id": inserted["id"],
                "context_game_id": inserted["context_game_id"],
                "last_message_at": inserted["last_message_at"],
                "last_message_id": inserted["last_message_id"],
                "created": True,
            }
        row = connection.execute(
            text("""
                SELECT id, context_game_id, last_message_at, last_message_id
                FROM concierge.sessions
                WHERE channel = :channel AND external_user_id = :user_id
                  AND status = 'active'
                FOR UPDATE
            """),
            {"channel": message.channel, "user_id": message.external_user_id},
        ).mappings().one()
        return {**row, "created": False}

    @staticmethod
    def _insert_message(
        connection: Connection,
        message: IncomingMessage,
        *,
        safe_text: str,
        session_id: UUID | None,
        status: str,
        lease_until: datetime | None,
        lease_token: UUID | None,
    ) -> None:
        connection.execute(
            text("""
                INSERT INTO concierge.messages (
                id, session_id, channel, external_update_id,
                external_message_id, external_user_id, external_chat_id,
                text, sent_at, received_at, status, lease_token, lease_until,
                correlation_id
            ) VALUES (
                :id, :session_id, :channel, :update_id,
                :message_id, :user_id, :chat_id,
                :text, :sent_at, :received_at, :status, :lease_token, :lease_until,
                :correlation_id
                )
            """),
            {
                "id": uuid4(),
                "session_id": session_id,
                "channel": message.channel,
                "update_id": message.update_id,
                "message_id": message.message_id,
                "user_id": message.external_user_id,
                "chat_id": message.external_chat_id,
                "text": safe_text,
                "sent_at": message.sent_at,
                "received_at": message.received_at,
                "status": status,
                "lease_until": lease_until,
                "lease_token": lease_token,
                "correlation_id": message.correlation_id,
            },
        )

    def complete_message(
        self,
        update_id: int,
        *,
        channel: str,
        session_id: UUID,
        processing_lease_token: UUID,
        reply_text: str,
        workflow_version: str,
        recommendation_context: dict[str, object] | None = None,
    ) -> None:
        try:
            with self.engine.begin() as connection:
                row = connection.execute(
                    text("""
                        SELECT id AS source_message_id, channel, external_chat_id
                        FROM concierge.messages
                        WHERE channel = :channel AND external_update_id = :update_id
                    """),
                    {"channel": channel, "update_id": update_id},
                ).mappings().one()
                completed = connection.execute(
                    text("""
                        UPDATE concierge.messages
                        SET status = 'processed', lease_token = NULL, lease_until = NULL
                        WHERE channel = :channel AND external_update_id = :update_id
                          AND status = 'processing' AND lease_token = :lease_token
                    """),
                    {
                        "channel": channel,
                        "update_id": update_id,
                        "lease_token": processing_lease_token,
                    },
                )
                if (completed.rowcount or 0) != 1:
                    raise SessionLeaseLost("session_processing_lease_lost")
                connection.execute(
                    text("""
                        UPDATE concierge.sessions
                        SET workflow_version = :workflow_version, updated_at = now()
                        WHERE id = :session_id
                    """),
                    {"workflow_version": workflow_version, "session_id": session_id},
                )
                connection.execute(
                    text("""
                        INSERT INTO concierge.outbox (
                            id, session_id, channel, external_update_id,
                            source_message_id, external_chat_id, text, status,
                            created_at, recommendation_context
                        ) VALUES (
                            :id, :session_id, :channel, :update_id,
                            :source_message_id, :chat_id, :text, 'pending', now(),
                            CAST(:recommendation_context AS jsonb)
                        ) ON CONFLICT (channel, external_update_id) DO NOTHING
                    """),
                    {
                        "id": uuid4(),
                        "session_id": session_id,
                        "channel": row["channel"],
                        "update_id": update_id,
                        "source_message_id": row["source_message_id"],
                        "chat_id": row["external_chat_id"],
                        "text": reply_text,
                        "recommendation_context": (
                            json.dumps(recommendation_context, ensure_ascii=False)
                            if recommendation_context is not None
                            else None
                        ),
                    },
                )
        except SQLAlchemyError as exc:
            raise SessionsUnavailable("concierge_session_storage_unavailable") from exc

    def claim_reply(
        self,
        update_id: int,
        *,
        channel: str,
        now: datetime,
        lease_seconds: int = 30,
    ) -> OutboxReply | None:
        try:
            with self.engine.begin() as connection:
                lease_token = uuid4()
                row = connection.execute(
                    text("""
                        WITH ready AS (
                            SELECT id
                            FROM concierge.outbox
                            WHERE channel = :channel
                              AND external_update_id = :update_id
                              AND (
                                  status = 'pending'
                                  OR (status = 'delivering' AND lease_until <= :now)
                              )
                            LIMIT 1
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE concierge.outbox AS outbox
                        SET status = 'delivering', lease_token = :lease_token,
                            lease_until = :lease_until
                        FROM ready
                        WHERE outbox.id = ready.id
                        RETURNING outbox.id, outbox.channel,
                                  outbox.external_update_id, outbox.external_chat_id,
                                  outbox.text, outbox.lease_token,
                                  outbox.recommendation_context
                    """),
                    {
                        "channel": channel,
                        "update_id": update_id,
                        "now": now,
                        "lease_token": lease_token,
                        "lease_until": now + timedelta(seconds=lease_seconds),
                    },
                ).mappings().first()
                return self._outbox_reply(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise SessionsUnavailable("concierge_session_storage_unavailable") from exc

    def claim_pending_replies(
        self,
        *,
        now: datetime,
        limit: int = 20,
        lease_seconds: int = 30,
    ) -> list[OutboxReply]:
        if limit < 1:
            return []
        try:
            with self.engine.begin() as connection:
                lease_token = uuid4()
                rows = connection.execute(
                    text("""
                        WITH ready AS (
                            SELECT id
                            FROM concierge.outbox
                            WHERE channel = 'telegram'
                              AND (
                                  status = 'pending'
                                  OR (status = 'delivering' AND lease_until <= :now)
                              )
                            ORDER BY created_at, id
                            LIMIT :limit
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE concierge.outbox AS outbox
                        SET status = 'delivering', lease_token = :lease_token,
                            lease_until = :lease_until
                        FROM ready
                        WHERE outbox.id = ready.id
                        RETURNING outbox.id, outbox.channel,
                                  outbox.external_update_id, outbox.external_chat_id,
                                  outbox.text, outbox.lease_token,
                                  outbox.recommendation_context
                    """),
                    {
                        "now": now,
                        "limit": limit,
                        "lease_token": lease_token,
                        "lease_until": now + timedelta(seconds=lease_seconds),
                    },
                ).mappings().all()
                return [self._outbox_reply(row) for row in rows]
        except SQLAlchemyError as exc:
            raise SessionsUnavailable("concierge_session_storage_unavailable") from exc

    def update_outbox_recommendation(
        self,
        update_id: int,
        *,
        channel: str,
        lease_token: UUID,
        reply_text: str,
        recommendation_context: dict[str, object],
    ) -> bool:
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    text("""
                        UPDATE concierge.outbox
                        SET text = :text,
                            recommendation_context = CAST(:recommendation_context AS jsonb)
                        WHERE channel = :channel AND external_update_id = :update_id
                          AND status = 'delivering' AND lease_token = :lease_token
                    """),
                    {
                        "text": reply_text,
                        "recommendation_context": json.dumps(
                            recommendation_context, ensure_ascii=False
                        ),
                        "channel": channel,
                        "update_id": update_id,
                        "lease_token": lease_token,
                    },
                )
                return (result.rowcount or 0) == 1
        except SQLAlchemyError as exc:
            raise SessionsUnavailable("concierge_session_storage_unavailable") from exc

    @staticmethod
    def _outbox_reply(row: RowMapping) -> OutboxReply:
        return OutboxReply(
            id=row["id"],
            channel=row["channel"],
            update_id=row["external_update_id"],
            chat_id=row["external_chat_id"],
            text=row["text"],
            lease_token=row["lease_token"],
            recommendation_context=row.get("recommendation_context"),
        )

    def mark_reply_delivered(
        self, update_id: int, *, channel: str, lease_token: UUID
    ) -> bool:
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    text("""
                        UPDATE concierge.outbox
                        SET status = 'delivered', lease_token = NULL,
                            lease_until = NULL, delivered_at = now()
                        WHERE channel = :channel AND external_update_id = :update_id
                          AND status = 'delivering' AND lease_token = :lease_token
                    """),
                    {
                        "channel": channel,
                        "update_id": update_id,
                        "lease_token": lease_token,
                    },
                )
                return (result.rowcount or 0) == 1
        except SQLAlchemyError as exc:
            raise SessionsUnavailable("concierge_session_storage_unavailable") from exc

    def release_reply(
        self,
        update_id: int,
        *,
        channel: str,
        lease_token: UUID,
        retry_delay_seconds: int = 30,
    ) -> bool:
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    text("""
                        UPDATE concierge.outbox
                        SET lease_until = now() + (:retry_delay_seconds * INTERVAL '1 second')
                        WHERE channel = :channel AND external_update_id = :update_id
                          AND status = 'delivering' AND lease_token = :lease_token
                    """),
                    {
                        "channel": channel,
                        "update_id": update_id,
                        "lease_token": lease_token,
                        "retry_delay_seconds": retry_delay_seconds,
                    },
                )
                return (result.rowcount or 0) == 1
        except SQLAlchemyError as exc:
            raise SessionsUnavailable("concierge_session_storage_unavailable") from exc

    def purge_expired_sessions(self, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        try:
            with self.engine.begin() as connection:
                session_ids = [
                    str(row[0])
                    for row in connection.execute(
                        text("""
                            SELECT id FROM concierge.sessions
                            WHERE updated_at < :cutoff
                            FOR UPDATE
                        """),
                        {"cutoff": cutoff},
                    )
                ]
                connection.execute(
                    text("""
                        DELETE FROM concierge.messages
                        WHERE session_id IS NULL AND received_at < :cutoff
                    """),
                    {"cutoff": cutoff},
                )
                if not session_ids:
                    return 0
                for table in (
                    "checkpoint_writes",
                    "checkpoint_blobs",
                    "checkpoints",
                ):
                    connection.execute(
                        text(f"""
                            DELETE FROM concierge.{table}
                            WHERE thread_id = ANY(:thread_ids)
                        """),
                        {"thread_ids": session_ids},
                    )
                result = connection.execute(
                    text("""
                        DELETE FROM concierge.sessions
                        WHERE id::text = ANY(:session_ids) AND updated_at < :cutoff
                    """),
                    {"session_ids": session_ids, "cutoff": cutoff},
                )
                return result.rowcount or 0
        except SQLAlchemyError as exc:
            raise SessionsUnavailable("concierge_retention_unavailable") from exc
