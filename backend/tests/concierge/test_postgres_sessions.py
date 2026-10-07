from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Lock
from uuid import uuid4

import pytest

from app.modules.concierge.adapters.postgres_sessions import (
    PostgresSessionRepository,
    SessionLeaseLost,
)
from app.modules.concierge.domain.session import IncomingMessage


class Result:
    def __init__(self, rows=(), rowcount: int = 0) -> None:  # type: ignore[no-untyped-def]
        self.rows = list(rows)
        self.rowcount = rowcount

    def mappings(self):  # type: ignore[no-untyped-def]
        return self

    def all(self):  # type: ignore[no-untyped-def]
        return self.rows

    def first(self):  # type: ignore[no-untyped-def]
        return self.rows[0] if self.rows else None

    def one(self):  # type: ignore[no-untyped-def]
        assert len(self.rows) == 1
        return self.rows[0]

    def scalar_one_or_none(self):  # type: ignore[no-untyped-def]
        if not self.rows:
            return None
        value = self.rows[0]
        if isinstance(value, (tuple, list)):
            return value[0]
        if isinstance(value, dict):
            return next(iter(value.values()))
        return value

    def scalar_one(self):  # type: ignore[no-untyped-def]
        assert len(self.rows) == 1
        return self.scalar_one_or_none()

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.rows)


class PostgresEngineFake:
    """Thread-safe SQL boundary fake for repository lease and purge contracts."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.queries: list[tuple[str, dict[str, object]]] = []
        self.session_ids = [uuid4()]
        self.outbox = {
            "id": uuid4(),
            "channel": "telegram",
            "external_update_id": 44,
            "external_chat_id": "12345",
            "text": "Resposta pendente",
            "status": "pending",
            "lease_token": None,
            "lease_until": None,
            "created_at": datetime.now(UTC),
        }

    @contextmanager
    def begin(self):  # type: ignore[no-untyped-def]
        yield self

    def execute(self, statement, parameters=None):  # type: ignore[no-untyped-def]
        sql = str(statement)
        args = dict(parameters or {})
        self.queries.append((sql, args))
        with self.lock:
            if "WITH ready AS" in sql:
                row = self.outbox
                if "channel = 'telegram'" in sql and row["channel"] != "telegram":
                    return Result()
                expected_update_id = args.get("update_id")
                matches_update = (
                    expected_update_id is None
                    or row["external_update_id"] == expected_update_id
                )
                available = row["status"] == "pending" or (
                    row["status"] == "delivering"
                    and row["lease_until"] <= args["now"]
                )
                if not (matches_update and available):
                    return Result()
                row.update(
                    status="delivering",
                    lease_token=args["lease_token"],
                    lease_until=args["lease_until"],
                )
                return Result([dict(row)])
            if "SET lease_until = now()" in sql:
                row = self.outbox
                matches = (
                    row["status"] == "delivering"
                    and row["lease_token"] == args["lease_token"]
                    and row["external_update_id"] == args["update_id"]
                    and row["channel"] == args["channel"]
                )
                if not matches:
                    return Result(rowcount=0)
                row["lease_until"] = datetime.now(UTC) + timedelta(
                    seconds=args["retry_delay_seconds"]
                )
                return Result(rowcount=1)
            if "SET status = 'delivered'" in sql:
                row = self.outbox
                matches = (
                    row["status"] == "delivering"
                    and row["lease_token"] == args["lease_token"]
                    and row["external_update_id"] == args["update_id"]
                    and row["channel"] == args["channel"]
                )
                if not matches:
                    return Result(rowcount=0)
                if "SET status = 'delivered'" in sql:
                    row.update(
                        status="delivered", lease_token=None, lease_until=None
                    )
                return Result(rowcount=1)
            if "SELECT id FROM concierge.sessions" in sql:
                return Result([(value,) for value in self.session_ids])
            if "DELETE FROM concierge.sessions" in sql:
                return Result(rowcount=len(self.session_ids))
            return Result()


def test_postgres_repository_claims_outbox_concurrently_with_fencing() -> None:
    engine = PostgresEngineFake()
    repository = PostgresSessionRepository(engine)  # type: ignore[arg-type]
    now = datetime.now(UTC)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(
                lambda _: repository.claim_pending_replies(now=now, limit=1),
                range(8),
            )
        )

    claimed = [reply for batch in claims for reply in batch]
    assert len(claimed) == 1
    assert any("channel = 'telegram'" in sql for sql, _ in engine.queries)
    first_lease = claimed[0]
    assert repository.claim_reply(
        first_lease.update_id, channel=first_lease.channel, now=now
    ) is None

    retry = repository.claim_reply(
        first_lease.update_id,
        channel=first_lease.channel,
        now=now + timedelta(seconds=31),
    )
    assert retry is not None
    assert retry.lease_token != first_lease.lease_token
    assert not repository.mark_reply_delivered(
        first_lease.update_id,
        channel=first_lease.channel,
        lease_token=first_lease.lease_token,
    )
    assert repository.mark_reply_delivered(
        retry.update_id,
        channel=retry.channel,
        lease_token=retry.lease_token,
    )


def test_postgres_repository_redrive_only_claims_telegram_rows() -> None:
    engine = PostgresEngineFake()
    engine.outbox["channel"] = "simulator"
    repository = PostgresSessionRepository(engine)  # type: ignore[arg-type]

    assert repository.claim_pending_replies(now=datetime.now(UTC), limit=1) == []
    assert any(
        "channel = 'telegram'" in sql for sql, _ in engine.queries
    )


def test_postgres_repository_releases_failed_outbox_delivery_for_redrive() -> None:
    engine = PostgresEngineFake()
    repository = PostgresSessionRepository(engine)  # type: ignore[arg-type]
    now = datetime.now(UTC)
    first = repository.claim_reply(44, channel="telegram", now=now)
    assert first is not None

    assert repository.release_reply(
        44, channel="telegram", lease_token=first.lease_token
    )
    assert repository.claim_pending_replies(
        now=now + timedelta(seconds=1), limit=1
    ) == []
    retry = repository.claim_pending_replies(now=now + timedelta(seconds=31), limit=1)
    assert len(retry) == 1
    assert retry[0].lease_token != first.lease_token


class MessageLeaseEngineFake:
    def __init__(self, message: IncomingMessage, session_id) -> None:  # type: ignore[no-untyped-def]
        self.lock = Lock()
        self.message = {
            "id": uuid4(),
            "session_id": session_id,
            "status": "processing",
            "lease_until": message.received_at - timedelta(seconds=1),
            "lease_token": uuid4(),
            "external_user_id": message.external_user_id,
            "external_chat_id": message.external_chat_id,
            "external_message_id": message.message_id,
            "channel": message.channel,
            "external_update_id": message.update_id,
            "correlation_id": message.correlation_id,
        }
        self.queries: list[str] = []

    @contextmanager
    def begin(self):  # type: ignore[no-untyped-def]
        yield self

    def execute(self, statement, parameters=None):  # type: ignore[no-untyped-def]
        sql = str(statement)
        args = dict(parameters or {})
        self.queries.append(sql)
        with self.lock:
            if "SELECT id, session_id, status, lease_until" in sql:
                return Result([dict(self.message)])
            if "SET status = 'processing'" in sql:
                self.message.update(
                    status="processing",
                    lease_token=args["lease_token"],
                    lease_until=args["lease_until"],
                )
                return Result(rowcount=1)
            if "SELECT context_game_id FROM concierge.sessions" in sql:
                return Result([None])
            if "SELECT EXISTS" in sql:
                return Result([bool(
                    self.message["status"] == "processing"
                    and self.message["lease_token"] == args["lease_token"]
                    and self.message["lease_until"] > args["now"]
                    and self.message["session_id"] == args["session_id"]
                )])
            if "SELECT id AS source_message_id" in sql:
                return Result([{
                    "source_message_id": self.message["id"],
                    "channel": self.message["channel"],
                    "external_chat_id": self.message["external_chat_id"],
                }])
            if "SET status = 'processed'" in sql:
                if self.message["lease_token"] != args["lease_token"]:
                    return Result(rowcount=0)
                self.message.update(
                    status="processed", lease_token=None, lease_until=None
                )
                return Result(rowcount=1)
            return Result(rowcount=1)


def test_postgres_repository_reclaims_stale_message_lease_and_fences_old_token() -> None:
    now = datetime.now(UTC)
    message = IncomingMessage(
        channel="telegram",
        external_user_id="12345",
        external_chat_id="12345",
        update_id=55,
        message_id=55,
        text="Mensagem recuperada",
        sent_at=now,
        received_at=now,
        correlation_id=uuid4(),
    )
    session_id = uuid4()
    engine = MessageLeaseEngineFake(message, session_id)
    repository = PostgresSessionRepository(engine)  # type: ignore[arg-type]
    stale_token = engine.message["lease_token"]

    reclaimed = repository.claim_message(
        message,
        safe_text=message.text,
        context_game_id=None,
        now=now,
        max_age_seconds=900,
    )

    assert reclaimed.status == "claimed"
    assert reclaimed.processing_lease_token is not None
    assert reclaimed.session_id == session_id
    assert not repository.is_processing_lease_current(
        message.update_id,
        channel=message.channel,
        session_id=session_id,
        processing_lease_token=stale_token,
        now=now,
    )
    assert repository.is_processing_lease_current(
        message.update_id,
        channel=message.channel,
        session_id=session_id,
        processing_lease_token=reclaimed.processing_lease_token,
        now=now,
    )
    with pytest.raises(SessionLeaseLost, match="session_processing_lease_lost"):
        repository.complete_message(
            message.update_id,
            channel=message.channel,
            session_id=session_id,
            processing_lease_token=stale_token,
            reply_text="Resposta velha",
            workflow_version="2.1.v1",
        )
    repository.complete_message(
        message.update_id,
        channel=message.channel,
        session_id=session_id,
        processing_lease_token=reclaimed.processing_lease_token,
        reply_text="Resposta atual",
        workflow_version="2.1.v1",
    )
    assert engine.message["status"] == "processed"


def test_postgres_repository_purge_cascades_session_data_and_checkpoints() -> None:
    engine = PostgresEngineFake()
    repository = PostgresSessionRepository(engine)  # type: ignore[arg-type]

    purged = repository.purge_expired_sessions(30)

    assert purged == 1
    sql = "\n".join(query for query, _ in engine.queries)
    assert "DELETE FROM concierge.checkpoint_writes" in sql
    assert "DELETE FROM concierge.checkpoint_blobs" in sql
    assert "DELETE FROM concierge.checkpoints" in sql
    assert "DELETE FROM concierge.sessions" in sql
    session_select = next(
        query
        for query, _ in engine.queries
        if "SELECT id FROM concierge.sessions" in query
    )
    session_delete = next(
        query
        for query, _ in engine.queries
        if "DELETE FROM concierge.sessions" in query
    )
    assert "FOR UPDATE" in session_select
    assert "updated_at < :cutoff" in session_delete
