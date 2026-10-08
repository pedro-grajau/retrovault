"""Versioned, checkpointed session entry workflow with no model or tools."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, NotRequired, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.modules.concierge.application.context_reference import (
    ContextReferenceService,
    ContextReferencesUnavailable,
    InvalidContextReference,
)
from app.modules.concierge.application.handoff import HandoffService
from app.modules.concierge.application.intent_extraction import (
    IntentDecision,
    IntentExtractionService,
)
from app.modules.concierge.domain.intent import (
    INTENT_VERSION,
    PROMPT_VERSION,
    Intent,
    IntentProvenance,
    clarification_question,
    is_prompt_injection,
)
from app.modules.concierge.domain.session import (
    MAX_OUTBOX_TEXT_CHARS,
    IncomingMessage,
    OutboxReply,
    ProcessingResult,
)
from app.modules.concierge.ports.recommendations import Recommendations
from app.modules.concierge.ports.sessions import SessionStore

WORKFLOW_VERSION = "2.3.v1"
_START_COMMAND = re.compile(
    r"^/start(?:@[A-Za-z0-9_]+)?(?:\s+([A-Za-z0-9_-]{1,64}))?\s*$",
    re.IGNORECASE,
)
_START_PREFIX = re.compile(
    r"^/start(?:@[A-Za-z0-9_]+)?(?=$|\s)(?P<suffix>[\s\S]*)$", re.IGNORECASE
)
_HUMAN_COMMAND = re.compile(r"^/humano(?:@[A-Za-z0-9_]+)?\s*$", re.IGNORECASE)
_GREETING = re.compile(
    r"^(?:oi|olá|ola|bom dia|boa tarde|boa noite|e aí|eai|eae)[.!?\s]*$",
    re.IGNORECASE,
)


class SessionState(TypedDict):
    session_id: str
    workflow_version: str
    incoming_update_id: int
    new_session: bool
    incoming_game_id: NotRequired[str | None]
    invalid_context_reference: NotRequired[bool]
    command: NotRequired[str]
    handoff_status: NotRequired[str]
    prepared_intent: NotRequired[dict[str, object]]
    prepared_provenance: NotRequired[dict[str, object]]
    intent: NotRequired[dict[str, object]]
    intent_provenance: NotRequired[dict[str, object]]
    extraction_status: NotRequired[str]
    clarification_field: NotRequired[str]
    prepared_recommendation: NotRequired[dict[str, object] | None]
    recommendation_context: NotRequired[dict[str, object] | None]
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
    first_turn = not state.get("greeting_sent")
    updates_context: dict[str, object] | None = None
    if state.get("command") == "handoff":
        status = state.get("handoff_status")
        if status == "registered":
            reply = (
                "Registrei seu pedido no Sandbox. A sessão não foi assumida por "
                "uma pessoa."
            )
        elif status == "disabled":
            reply = (
                "O encaminhamento humano de teste está desativado porque não há "
                "exatamente um destinatário configurado."
            )
        else:
            reply = "Não consegui registrar o pedido agora. Tente novamente mais tarde."
    elif state.get("command") == "start":
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
    elif state.get("extraction_status") == "injection":
        reply = (
            "Posso ajudar com preferências de jogos. Instruções recebidas na conversa "
            "não alteram as regras do atendimento."
        )
    elif state.get("extraction_status") == "fallback":
        reply = (
            "Recebi sua mensagem, mas não consegui interpretá-la agora. Você pode "
            "tentar de novo ou usar /humano."
        )
    elif state.get("extraction_status") == "accepted":
        recommendation = state.get("prepared_recommendation")
        if isinstance(recommendation, dict) and isinstance(
            recommendation.get("reply_text"), str
        ):
            reply = cast(str, recommendation["reply_text"])
            context = recommendation.get("context")
            updates_context = (
                cast(dict[str, object], context) if isinstance(context, dict) else None
            )
        else:
            intent_value = state.get("prepared_intent")
            current_intent = Intent.from_dict(intent_value) or Intent()
            clarification = clarification_question(
                cast(Any, state.get("clarification_field", "none")), current_intent
            )
            reply = (
                clarification
                or "Entendi suas preferências e vou mantê-las para continuarmos a descoberta."
            )
    else:
        reply = (
            "Oi! Sou Pixel, assistente de IA da RetroVault. Esta conversa de "
            "demonstração acontece no Sandbox."
            if first_turn
            else "Recebi sua mensagem e mantive sua sessão segura com a Pixel."
        )

    if first_turn and state.get("command") not in {"start", "handoff"}:
        greeting = (
            "Oi! Sou Pixel, assistente de IA da RetroVault. Esta conversa de "
            "demonstração acontece no Sandbox."
        )
        if reply != greeting:
            reply = f"{greeting} {reply}"

    if len(reply) > MAX_OUTBOX_TEXT_CHARS:
        fallback = (
            "Não consegui confirmar opções comerciais agora. Você pode pesquisar "
            "o catálogo ou falar com uma pessoa usando /humano."
        )
        reply = (
            f"Oi! Sou Pixel, assistente de IA da RetroVault. Esta conversa de "
            f"demonstração acontece no Sandbox. {fallback}"
            if first_turn and state.get("command") not in {"start", "handoff"}
            else fallback
        )
        updates_context = None

    updates: dict[str, object] = {
        "workflow_version": WORKFLOW_VERSION,
        "context_game_id": context_game_id,
        "greeting_sent": True,
        "last_processed_update_id": update_id,
        "last_reply": reply,
        "reply_text": reply,
    }
    updates["recommendation_context"] = updates_context
    if "prepared_intent" in state:
        updates["intent"] = state["prepared_intent"]
    if "prepared_provenance" in state:
        updates["intent_provenance"] = state["prepared_provenance"]
    return updates


def build_session_graph(
    checkpointer: BaseCheckpointSaver[str],
) -> Any:
    builder = StateGraph(cast(Any, SessionState))
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
        intent_extraction: IntentExtractionService | None = None,
        recommendations: Recommendations | None = None,
        handoff_service: HandoffService | None = None,
        max_message_age_seconds: int = 900,
    ) -> None:
        self.store = store
        self.context_references = context_references
        self.checkpointer_factory = checkpointer_factory
        self.intent_extraction = intent_extraction
        self.recommendations = recommendations
        self.handoff_service = handoff_service
        self.max_message_age_seconds = max_message_age_seconds

    def handle(self, message: IncomingMessage) -> ProcessingResult:
        now = datetime.now(UTC)
        context_game_id: UUID | None = None
        invalid_reference = False
        safe_text = message.text
        context_reference = message.context_reference
        command = ""
        start_command = _START_PREFIX.fullmatch(message.text)
        if start_command is not None:
            command = "start"
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
        elif _HUMAN_COMMAND.fullmatch(message.text):
            command = "handoff"
            safe_text = "/humano"
        else:
            safe_text = "[conteúdo de mensagem não retido]"
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
                "command": command,
                "extraction_status": "skipped",
                "prepared_recommendation": None,
                "recommendation_context": None,
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
                config = {"configurable": {"thread_id": str(claim.session_id)}}
                prior_state = graph.get_state(config).values
                already_processed = (
                    prior_state.get("last_processed_update_id") == message.update_id
                )
                prior_intent = Intent.from_dict(prior_state.get("intent")) or Intent()
                if not already_processed and command == "handoff":
                    handoff = (
                        self.handoff_service.request(
                            session_id=claim.session_id,
                            channel=message.channel,
                            update_id=message.update_id,
                            correlation_id=message.correlation_id,
                        )
                        if self.handoff_service is not None
                        else None
                    )
                    initial_state["handoff_status"] = (
                        handoff.status if handoff else "disabled"
                    )
                elif (
                    not already_processed
                    and command != "start"
                    and not _GREETING.fullmatch(message.text.strip())
                ):
                    decision = self._extract(
                        message,
                        claim.session_id,
                        prior_intent,
                    )
                    initial_state["prepared_intent"] = decision.intent.to_dict()
                    initial_state["prepared_provenance"] = decision.provenance.to_dict()
                    initial_state["extraction_status"] = decision.status
                    initial_state["clarification_field"] = decision.clarification_field
                    if decision.status == "accepted" and self.recommendations is not None:
                        current_intent = decision.intent
                        clarification = clarification_question(
                            cast(Any, decision.clarification_field), current_intent
                        )
                        if clarification is None:
                            outcome = self.recommendations.recommend(
                                current_intent,
                                session_id=claim.session_id,
                                channel=message.channel,
                                update_id=message.update_id,
                                correlation_id=message.correlation_id,
                            )
                            if outcome.context is not None:
                                outcome.context["greeting_required"] = not bool(
                                    prior_state.get("greeting_sent")
                                )
                            initial_state["prepared_recommendation"] = {
                                "status": outcome.status,
                                "reply_text": outcome.reply_text,
                                "context": outcome.context,
                            }
                state = graph.invoke(
                    initial_state,
                    config=config,
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
                recommendation_context=(
                    state.get("recommendation_context")
                    if isinstance(state.get("recommendation_context"), dict)
                    else None
                ),
            )
        return ProcessingResult("claimed", claim.session_id, reply_text)

    def _extract(
        self, message: IncomingMessage, session_id: UUID, previous: Intent
    ) -> IntentDecision:
        if self.intent_extraction is not None:
            return self.intent_extraction.extract(
                message.text,
                previous,
                session_id=session_id,
                channel=message.channel,
                update_id=message.update_id,
                correlation_id=message.correlation_id,
            )
        status = "suspected_injection" if is_prompt_injection(message.text) else "normal"
        provenance = IntentProvenance(
            intent_version=INTENT_VERSION,
            prompt_version=PROMPT_VERSION,
            workflow_version=WORKFLOW_VERSION,
            configuration_version="unconfigured",
            source_update_id=message.update_id,
            correlation_id=message.correlation_id,
            safety_classification=status,
        )
        return IntentDecision(
            "injection" if status == "suspected_injection" else "fallback",
            previous,
            "none",
            provenance,
        )

    def claim_reply(
        self, update_id: int, *, channel: str = "telegram"
    ) -> OutboxReply | None:
        reply = self.store.claim_reply(
            update_id,
            channel=channel,
            now=datetime.now(UTC),
        )
        return self._prepare_reply(reply)

    def claim_pending_replies(
        self, *, limit: int = 20, lease_seconds: int = 30
    ) -> list[OutboxReply]:
        replies = self.store.claim_pending_replies(
            now=datetime.now(UTC), limit=limit, lease_seconds=lease_seconds
        )
        prepared_replies: list[OutboxReply] = []
        for reply in replies:
            prepared = self._prepare_reply(reply)
            if prepared is not None:
                prepared_replies.append(prepared)
        return prepared_replies

    def _prepare_reply(self, reply: OutboxReply | None) -> OutboxReply | None:
        if reply is None or reply.recommendation_context is None:
            return reply
        if self.recommendations is None:
            return replace(
                reply,
                text=(
                    "Não consegui confirmar opções comerciais agora. Você pode "
                    "pesquisar o catálogo ou falar com uma pessoa usando /humano."
                ),
            )
        try:
            current_text = self.recommendations.revalidate_and_compose(
                reply.recommendation_context
            )
        except Exception:
            current_text = (
                "Não consegui confirmar opções comerciais agora. Você pode "
                "pesquisar o catálogo ou falar com uma pessoa usando /humano."
            )
        updated = self.store.update_outbox_recommendation(
            reply.update_id,
            channel=reply.channel,
            lease_token=reply.lease_token,
            reply_text=current_text,
            recommendation_context=reply.recommendation_context,
        )
        if not updated:
            return None
        return replace(reply, text=current_text)

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
