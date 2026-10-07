"""First-class in-memory adapter used by CI and local session simulations."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
from _thread import LockType
from threading import Lock
from typing import Any, Generator, Protocol
from uuid import UUID, uuid4

from app.modules.concierge.domain.session import (
    IncomingMessage,
    MessageClaim,
    OutboxReply,
    ProcessingResult,
)


class SessionWorkflowPort(Protocol):
    def handle(self, message: IncomingMessage) -> ProcessingResult: ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._sessions: dict[tuple[str, str], dict[str, Any]] = {}
        self._terminal_sessions: dict[UUID, dict[str, Any]] = {}
        self._messages: dict[tuple[str, int], dict[str, Any]] = {}
        self._context_reference_uses: dict[str, UUID] = {}
        self._replay_anomalies: set[tuple[str, int, str, str, int]] = set()
        self._session_lock_guard = Lock()
        self._session_processing_locks: dict[UUID, tuple[LockType, int]] = {}

    @contextmanager
    def session_processing_lock(
        self, session_id: UUID
    ) -> Generator[None, None, None]:
        with self._session_lock_guard:
            lock, users = self._session_processing_locks.get(
                session_id, (Lock(), 0)
            )
            self._session_processing_locks[session_id] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._session_lock_guard:
                _, users = self._session_processing_locks[session_id]
                if users == 1:
                    del self._session_processing_locks[session_id]
                else:
                    self._session_processing_locks[session_id] = (lock, users - 1)

    def is_processing_lease_current(
        self,
        update_id: int,
        *,
        channel: str,
        session_id: UUID,
        processing_lease_token: UUID,
        now: datetime,
    ) -> bool:
        with self._lock:
            record = self._messages.get((channel, update_id))
            return bool(
                record is not None
                and record.get("session_id") == session_id
                and record.get("status") == "processing"
                and record.get("processing_lease_token") == processing_lease_token
                and record.get("lease_until") is not None
                and record["lease_until"] > now
            )

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
        with self._lock:
            message_key = (message.channel, message.update_id)
            existing = self._messages.get(message_key)
            if existing is not None:
                if (
                    existing["external_user_id"] != message.external_user_id
                    or existing["external_chat_id"] != message.external_chat_id
                    or existing["external_message_id"] != message.message_id
                ):
                    self._replay_anomalies.add(
                        (
                            message.channel,
                            message.update_id,
                            hashlib.sha256(message.external_user_id.encode()).hexdigest(),
                            hashlib.sha256(message.external_chat_id.encode()).hexdigest(),
                            message.message_id,
                        )
                    )
                    return MessageClaim("reconciliation", existing["session_id"])
                status = existing["status"]
                if status == "processed":
                    return MessageClaim(
                        "duplicate", existing["session_id"]
                    )
                if status == "reconciliation":
                    return MessageClaim("reconciliation")
                if existing["lease_until"] > now:
                    return MessageClaim("in_progress", existing["session_id"])
                existing["lease_until"] = now + timedelta(minutes=2)
                processing_lease_token = uuid4()
                existing["processing_lease_token"] = processing_lease_token
                return MessageClaim(
                    "claimed",
                    existing["session_id"],
                    existing["context_game_id"],
                    bool(existing["created_session"]),
                    context_reference_replayed=bool(
                        existing.get("context_reference_replayed")
                    ),
                    processing_lease_token=processing_lease_token,
                )

            if message.sent_at < now - timedelta(seconds=max_age_seconds):
                self._messages[message_key] = {
                    "status": "reconciliation",
                    "session_id": None,
                    "external_user_id": message.external_user_id,
                    "external_chat_id": message.external_chat_id,
                    "external_message_id": message.message_id,
                    "safe_text": safe_text,
                    "received_at": message.received_at,
                }
                return MessageClaim("reconciliation")

            key = (message.channel, message.external_user_id)
            session = self._sessions.get(key)
            if session is not None and session["status"] == "terminal":
                self._terminal_sessions[session["id"]] = session
                del self._sessions[key]
                session = None
            if session is not None and (
                message.sent_at < session["last_message_at"]
                or (
                    message.sent_at == session["last_message_at"]
                    and message.message_id <= session["last_message_id"]
                )
            ):
                self._messages[message_key] = {
                    "status": "reconciliation",
                    "session_id": session["id"],
                    "external_user_id": message.external_user_id,
                    "external_chat_id": message.external_chat_id,
                    "external_message_id": message.message_id,
                    "safe_text": safe_text,
                    "received_at": message.received_at,
                }
                return MessageClaim("reconciliation", session["id"])

            if session is not None and any(
                record["session_id"] == session["id"]
                and record["status"] == "processing"
                and record["lease_until"] > now
                for record in self._messages.values()
            ):
                return MessageClaim("in_progress", session["id"])

            context_reference_replayed = False
            if context_reference_hash is not None:
                context_reference_replayed = (
                    context_reference_hash in self._context_reference_uses
                )
                if context_reference_replayed:
                    context_game_id = None

            created = session is None
            if session is None:
                session = {
                    "id": uuid4(),
                    "status": "active",
                    "context_game_id": context_game_id,
                    "last_message_at": message.sent_at,
                    "last_message_id": message.message_id,
                    "external_chat_id": message.external_chat_id,
                }
                self._sessions[key] = session
            else:
                session["last_message_at"] = message.sent_at
                session["last_message_id"] = message.message_id
                if context_game_id is not None:
                    session["context_game_id"] = context_game_id
            if context_reference_hash is not None and not context_reference_replayed:
                self._context_reference_uses[context_reference_hash] = session["id"]
            processing_lease_token = uuid4()
            self._messages[message_key] = {
                "status": "processing",
                "session_id": session["id"],
                "external_user_id": message.external_user_id,
                "external_chat_id": message.external_chat_id,
                "external_message_id": message.message_id,
                "context_game_id": session["context_game_id"],
                "created_session": created,
                "safe_text": safe_text,
                "received_at": message.received_at,
                "lease_until": now + timedelta(minutes=2),
                "processing_lease_token": processing_lease_token,
                "context_reference_replayed": context_reference_replayed,
                "reply": None,
                "delivery_status": None,
                "delivery_lease_token": None,
                "delivery_lease_until": None,
            }
            return MessageClaim(
                "claimed",
                session["id"],
                session["context_game_id"],
                created,
                context_reference_replayed=context_reference_replayed,
                processing_lease_token=processing_lease_token,
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
    ) -> None:
        with self._lock:
            record = self._messages[(channel, update_id)]
            if record["session_id"] != session_id:
                raise ValueError("session_message_mismatch")
            if (
                record["status"] != "processing"
                or record["processing_lease_token"] != processing_lease_token
            ):
                raise RuntimeError("session_processing_lease_lost")
            record.update(
                status="processed",
                processing_lease_token=None,
                reply=reply_text,
                workflow_version=workflow_version,
                delivery_status="pending",
            )

    def claim_reply(
        self,
        update_id: int,
        *,
        channel: str,
        now: datetime,
        lease_seconds: int = 30,
    ) -> OutboxReply | None:
        with self._lock:
            key = (channel, update_id)
            record = self._messages.get(key)
            if record is None or not self._claimable_reply(record, now):
                return None
            return self._lease_reply(record, channel, update_id, now, lease_seconds)

    def claim_pending_replies(
        self,
        *,
        now: datetime,
        limit: int = 20,
        lease_seconds: int = 30,
    ) -> list[OutboxReply]:
        with self._lock:
            replies: list[OutboxReply] = []
            records = sorted(
                self._messages.items(),
                key=lambda item: item[1].get("received_at", datetime.min.replace(tzinfo=UTC)),
            )
            for (channel, update_id), record in records:
                if len(replies) >= limit:
                    break
                if channel == "telegram" and self._claimable_reply(record, now):
                    replies.append(
                        self._lease_reply(record, channel, update_id, now, lease_seconds)
                    )
            return replies

    @staticmethod
    def _claimable_reply(record: dict[str, Any], now: datetime) -> bool:
        return record.get("delivery_status") == "pending" or (
            record.get("delivery_status") == "delivering"
            and record.get("delivery_lease_until") is not None
            and record["delivery_lease_until"] <= now
        )

    @staticmethod
    def _lease_reply(
        record: dict[str, Any],
        channel: str,
        update_id: int,
        now: datetime,
        lease_seconds: int,
    ) -> OutboxReply:
        token = uuid4()
        record["delivery_status"] = "delivering"
        record["delivery_lease_token"] = token
        record["delivery_lease_until"] = now + timedelta(seconds=lease_seconds)
        return OutboxReply(
            id=uuid4(),
            channel=channel,
            update_id=update_id,
            chat_id=record["external_chat_id"],
            text=record["reply"],
            lease_token=token,
        )

    def mark_reply_delivered(
        self, update_id: int, *, channel: str, lease_token: UUID
    ) -> bool:
        with self._lock:
            record = self._messages.get((channel, update_id))
            if (
                record is None
                or record.get("delivery_status") != "delivering"
                or record.get("delivery_lease_token") != lease_token
            ):
                return False
            record["delivery_status"] = "delivered"
            record["delivery_lease_token"] = None
            record["delivery_lease_until"] = None
            record["delivered"] = True
            return True

    def release_reply(
        self,
        update_id: int,
        *,
        channel: str,
        lease_token: UUID,
        retry_delay_seconds: int = 30,
    ) -> bool:
        with self._lock:
            record = self._messages.get((channel, update_id))
            if (
                record is None
                or record.get("delivery_status") != "delivering"
                or record.get("delivery_lease_token") != lease_token
            ):
                return False
            record["delivery_lease_until"] = datetime.now(UTC) + timedelta(
                seconds=retry_delay_seconds
            )
            return True

    def purge_expired_sessions(self, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        with self._lock:
            expired_ids: set[UUID] = set()
            expired_active = [
                key
                for key, session in self._sessions.items()
                if session["last_message_at"] < cutoff
            ]
            for key in expired_active:
                expired_ids.add(self._sessions.pop(key)["id"])
            expired_terminal = [
                session_id
                for session_id, session in self._terminal_sessions.items()
                if session["last_message_at"] < cutoff
            ]
            for session_id in expired_terminal:
                expired_ids.add(self._terminal_sessions.pop(session_id)["id"])
            for update_id, message in tuple(self._messages.items()):
                if message["session_id"] in expired_ids:
                    del self._messages[update_id]
            self._context_reference_uses = {
                digest: session_id
                for digest, session_id in self._context_reference_uses.items()
                if session_id not in expired_ids
            }
            for update_id, message in tuple(self._messages.items()):
                if (
                    message["session_id"] is None
                    and message.get("received_at") is not None
                    and message["received_at"] < cutoff
                ):
                    del self._messages[update_id]
            return len(expired_ids)


class SessionSimulator:
    """Feed neutral messages through the same workflow used by Telegram."""

    def __init__(self, workflow: SessionWorkflowPort) -> None:
        self.workflow = workflow
        self._counter = 0

    def send(
        self, user_id: str, text: str, *, game_reference: str | None = None
    ) -> ProcessingResult:
        if (
            not isinstance(text, str)
            or not text
            or len(text) > 4096
            or any(
                ord(character) < 32 and character not in "\t\r\n"
                for character in text
            )
        ):
            raise ValueError("invalid_message_text")
        self._counter += 1
        now = datetime.now(UTC)
        message = IncomingMessage(
            channel="simulator",
            external_user_id=user_id,
            external_chat_id=user_id,
            update_id=self._counter,
            message_id=self._counter,
            text=text,
            sent_at=now,
            received_at=now,
            correlation_id=uuid4(),
            context_reference=game_reference,
        )
        return self.workflow.handle(message)
