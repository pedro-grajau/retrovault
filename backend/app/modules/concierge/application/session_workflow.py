"""Versioned, checkpointed session entry workflow with no model or tools."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, NotRequired
from uuid import NAMESPACE_URL, UUID, uuid5

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from typing_extensions import TypedDict

from app.modules.concierge.application.context_reference import (
    ContextReferenceService,
    ContextReferencesUnavailable,
    InvalidContextReference,
)
from app.modules.concierge.domain.session import (
    IncomingMessage,
    OutboxReply,
    ProcessingResult,
)
from app.modules.concierge.ports.sessions import SessionStore

WORKFLOW_VERSION = "2.1.v1"
_START_COMMAND = re.compile(
    r"^/start(?:@[A-Za-z0-9_]+)?(?:\s+([A-Za-z0-9_-]{1,64}))?\s*$",
    re.IGNORECASE,
)
_START_PREFIX = re.compile(
    r"^/start(?:@[A-Za-z0-9_]+)?(?=$|\s)(?P<suffix>[\s\S]*)$", re.IGNORECASE
)


class SessionState(TypedDict):
    session_id: str
    workflow_version: str
    incoming_update_id: int
    new_session: bool
    incoming_game_id: NotRequired[str | None]
    invalid_context_reference: NotRequired[bool]
    context_game_id: NotRequired[str | None]
    greeting_sent: NotRequired[bool]
    last_processed_update_id: NotRequired[int]
    last_reply: NotRequired[str | None]
    reply_text: NotRequired[str | None]


CheckpointerFactory = Callable[
    [], AbstractContextManager[BaseCheckpointSaver[str]]
]


def _entry_node(state: SessionState) -> dict[str, object]:
    update_id = state["incoming_update_id"]
    if state.get("last_processed_update_id") == update_id:
        return {"reply_text": state.get("last_reply")}

    context_game_id = state.get("incoming_game_id") or state.get("context_game_id")
    if not state.get("greeting_sent"):
        if state.get("invalid_context_reference"):
            reply = (
                "Oi! Sou Pixel, assistente de IA da RetroVault. Esta é uma conversa "
                "de demonstração no Sandbox. O link do jogo não pôde ser validado; "
                "vou iniciar sem associar esse contexto."
            )
        elif context_game_id:
            reply = (
                "Oi! Sou Pixel, assistente de IA da RetroVault. Esta conversa de "
                "demonstração acontece no Sandbox, e recebi o contexto do jogo que "
                "você escolheu."
            )
        else:
            reply = (
                "Oi! Sou Pixel, assistente de IA da RetroVault. Esta conversa de "
                "demonstração acontece no Sandbox."
            )
    else:
        reply = "Recebi sua mensagem e mantive sua sessão segura com a Pixel."

    return {
        "workflow_version": WORKFLOW_VERSION,
        "context_game_id": context_game_id,
        "greeting_sent": True,
        "last_processed_update_id": update_id,
        "last_reply": reply,
        "reply_text": reply,
    }


def build_session_graph(
    checkpointer: BaseCheckpointSaver[str],
) -> CompiledStateGraph[SessionState, Any, SessionState, SessionState]:
    builder = StateGraph(SessionState)
    builder.add_node("secure_entry", _entry_node)
    builder.add_edge(START, "secure_entry")
    builder.add_edge("secure_entry", END)
    return builder.compile(checkpointer=checkpointer)


class SessionWorkflow:
    def __init__(
        self,
        store: SessionStore,
        context_references: ContextReferenceService,
        checkpointer_factory: CheckpointerFactory,
        *,
        max_message_age_seconds: int = 900,
    ) -> None:
        self.store = store
        self.context_references = context_references
        self.checkpointer_factory = checkpointer_factory
        self.max_message_age_seconds = max_message_age_seconds

    def handle(self, message: IncomingMessage) -> ProcessingResult:
        now = datetime.now(UTC)
        context_game_id: UUID | None = None
        invalid_reference = False
        safe_text = message.text
        context_reference = message.context_reference
        start_command = _START_PREFIX.fullmatch(message.text)
        if start_command is not None:
            match = _START_COMMAND.fullmatch(message.text)
            if context_reference is None and match is not None:
                context_reference = match.group(1)
            has_parameter = bool(start_command.group("suffix").strip())
            if context_reference is None and has_parameter:
                invalid_reference = True
            safe_text = (
                "/start [context parameter redacted]"
                if has_parameter
                else "/start"
            )
        if context_reference:
            try:
                context_game_id = self.context_references.validate(context_reference).game_id
            except InvalidContextReference:
                invalid_reference = True
            except ContextReferencesUnavailable as exc:
                if str(exc) != "context_reference_unavailable":
                    raise
                invalid_reference = True
            safe_text = "/start [context reference redacted]"

        context_reference_hash = (
            hashlib.sha256(context_reference.encode("utf-8")).hexdigest()
            if context_reference and context_game_id is not None
            else None
        )
        # Lock by channel and user before claiming. This also covers the first
        # message, when no durable session UUID exists yet, and keeps a second
        # worker from reclaiming an expired lease while the first graph writes
        # its checkpoint.
        lock_id = uuid5(
            NAMESPACE_URL,
            f"retrovault-concierge:{message.channel}:{message.external_user_id}",
        )
        with self.store.session_processing_lock(lock_id):
            claim = self.store.claim_message(
                message,
                safe_text=safe_text,
                context_game_id=context_game_id,
                now=now,
                max_age_seconds=self.max_message_age_seconds,
                context_reference_hash=context_reference_hash,
            )
            if claim.status != "claimed":
                return ProcessingResult(claim.status, claim.session_id, claim.reply_text)
            if claim.session_id is None:
                raise RuntimeError("claimed_session_missing")
            if claim.processing_lease_token is None:
                raise RuntimeError("claimed_session_lease_missing")

            initial_state: SessionState = {
                "session_id": str(claim.session_id),
                "workflow_version": WORKFLOW_VERSION,
                "incoming_update_id": message.update_id,
                "new_session": claim.created_session,
                "incoming_game_id": (
                    str(claim.context_game_id) if claim.context_game_id else None
                ),
                "invalid_context_reference": (
                    invalid_reference or claim.context_reference_replayed
                ),
            }
            if not self.store.is_processing_lease_current(
                message.update_id,
                channel=message.channel,
                session_id=claim.session_id,
                processing_lease_token=claim.processing_lease_token,
                now=datetime.now(UTC),
            ):
                return ProcessingResult("in_progress", claim.session_id)
            with self.checkpointer_factory() as checkpointer:
                graph = build_session_graph(checkpointer)
                state = graph.invoke(
                    initial_state,
                    config={"configurable": {"thread_id": str(claim.session_id)}},
                )
            reply_text = state.get("reply_text")
            if not isinstance(reply_text, str) or not reply_text:
                raise RuntimeError("session_reply_missing")
            self.store.complete_message(
                message.update_id,
                channel=message.channel,
                session_id=claim.session_id,
                processing_lease_token=claim.processing_lease_token,
                reply_text=reply_text,
                workflow_version=WORKFLOW_VERSION,
            )
        return ProcessingResult("claimed", claim.session_id, reply_text)

    def claim_reply(
        self, update_id: int, *, channel: str = "telegram"
    ) -> OutboxReply | None:
        return self.store.claim_reply(
            update_id,
            channel=channel,
            now=datetime.now(UTC),
        )

    def mark_reply_delivered(
        self, update_id: int, *, channel: str = "telegram", lease_token: UUID
    ) -> bool:
        return self.store.mark_reply_delivered(
            update_id, channel=channel, lease_token=lease_token
        )

    def release_reply(
        self,
        update_id: int,
        *,
        channel: str = "telegram",
        lease_token: UUID,
        retry_delay_seconds: int = 30,
    ) -> bool:
        return self.store.release_reply(
            update_id,
            channel=channel,
            lease_token=lease_token,
            retry_delay_seconds=retry_delay_seconds,
        )
