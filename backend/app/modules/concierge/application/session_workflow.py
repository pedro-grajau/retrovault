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
from app.modules.concierge.application.demand import DemandService
from app.modules.concierge.application.handoff import HandoffService
from app.modules.concierge.application.intent_extraction import (
    IntentDecision,
    IntentExtractionService,
)
from app.modules.concierge.domain.demand import clean_description, normalize
from app.modules.concierge.domain.intent import (
    INTENT_VERSION,
    PROMPT_VERSION,
    REFINEMENT_VERSION,
    Intent,
    IntentProvenance,
    clarification_question,
    is_prompt_injection,
    redact_sensitive_text,
)
from app.modules.concierge.domain.recommendation import RecommendationPlan
from app.modules.concierge.domain.session import (
    MAX_OUTBOX_TEXT_CHARS,
    IncomingMessage,
    OutboxReply,
    ProcessingResult,
)
from app.modules.concierge.ports.recommendations import (
    RecommendationOutcome,
    Recommendations,
)
from app.modules.concierge.ports.sessions import SessionStore

WORKFLOW_VERSION = "2.5.v1"
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
_DEMAND_MODE_ALIASES = {
    "compra": "purchase",
    "comprar": "purchase",
    "purchase": "purchase",
    "aluguel": "rental",
    "alugar": "rental",
    "rental": "rental",
}


def _is_demand_answer(text: str, title: str | None, platform: str | None) -> bool:
    answer = normalize(text)
    for value in (title, platform):
        if value:
            answer = answer.replace(normalize(value), " ")
    words = set(re.findall(r"[a-z]+", answer))
    # A short field answer may continue the draft; a fresh search must retain
    # normal recommendation routing even if it mentions a platform or mode.
    return words <= set(_DEMAND_MODE_ALIASES) | {
        "eu",
        "quero",
        "prefiro",
        "para",
        "por",
        "favor",
        "a",
        "o",
        "um",
        "uma",
        "de",
        "e",
        "ou",
        "na",
        "no",
        "em",
        "plataforma",
        "titulo",
        "jogo",
        "modalidade",
        "sim",
        "isso",
    }


def _restored_demand_draft(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    title = value.get("title")
    platform = value.get("platform")
    mode = value.get("mode")
    restored: dict[str, str] = {}
    if isinstance(title, str) and title:
        restored["title"] = clean_description(title)
    if isinstance(platform, str) and platform:
        restored["platform"] = clean_description(platform, maximum=48)
    if isinstance(mode, str) and mode in {"purchase", "rental"}:
        restored["mode"] = mode
    return restored


def _demand_clarification(draft: dict[str, str]) -> str:
    if not draft.get("title"):
        return "Qual é o título que você quer acompanhar?"
    if not draft.get("platform"):
        return f"Qual é a plataforma de {draft['title']}?"
    if not draft.get("mode"):
        return "Você quer receber o aviso para compra ou aluguel?"
    raise ValueError("complete_demand_draft_has_no_clarification")


class SessionState(TypedDict):
    session_id: str
    workflow_version: str
    incoming_update_id: int
    new_session: bool
    incoming_game_id: NotRequired[str | None]
    invalid_context_reference: NotRequired[bool]
    command: NotRequired[str]
    demand_reply: NotRequired[str | None]
    demand_draft: NotRequired[dict[str, str] | None]
    handoff_status: NotRequired[str]
    prepared_intent: NotRequired[dict[str, object]]
    prepared_provenance: NotRequired[dict[str, object]]
    intent: NotRequired[dict[str, object]]
    intent_provenance: NotRequired[dict[str, object]]
    extraction_status: NotRequired[str]
    clarification_field: NotRequired[str]
    force_clarification: NotRequired[bool]
    prepared_recommendation: NotRequired[dict[str, object] | None]
    prepared_refinement_state: NotRequired[dict[str, object]]
    recommendation_context: NotRequired[dict[str, object] | None]
    last_recommendation_context: NotRequired[dict[str, object] | None]
    refinement_state: NotRequired[dict[str, object]]
    rejection_ambiguous: NotRequired[bool]
    context_game_id: NotRequired[str | None]
    greeting_sent: NotRequired[bool]
    last_processed_update_id: NotRequired[int]
    last_reply: NotRequired[str | None]
    reply_text: NotRequired[str | None]


CheckpointerFactory = Callable[[], AbstractContextManager[BaseCheckpointSaver[str]]]


def _entry_node(state: SessionState) -> dict[str, object]:
    update_id = state["incoming_update_id"]
    if state.get("last_processed_update_id") == update_id:
        return {"reply_text": state.get("last_reply")}

    context_game_id = state.get("incoming_game_id") or state.get("context_game_id")
    first_turn = not state.get("greeting_sent")
    updates_context: dict[str, object] | None = None
    if state.get("demand_reply"):
        reply = cast(str, state["demand_reply"])
    elif state.get("command") == "handoff":
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
        if state.get("rejection_ambiguous"):
            reply = (
                "Quais opções você quer recusar? Pode me dizer o título ou o número "
                "que apareceu na lista."
            )
        elif isinstance(recommendation, dict) and isinstance(
            recommendation.get("reply_text"), str
        ):
            reply = cast(str, recommendation["reply_text"])
            context = recommendation.get("context")
            updates_context = (
                cast(dict[str, object], context)
                if recommendation.get("status") == "accepted"
                and isinstance(context, dict)
                else None
            )
        else:
            intent_value = state.get("prepared_intent")
            current_intent = Intent.from_dict(intent_value) or Intent()
            clarification = clarification_question(
                cast(Any, state.get("clarification_field", "none")),
                current_intent,
                force=bool(state.get("force_clarification")),
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
    if "prepared_refinement_state" in state:
        updates["refinement_state"] = state["prepared_refinement_state"]
    if "demand_draft" in state:
        updates["demand_draft"] = state["demand_draft"]
    return updates


def build_session_graph(
    checkpointer: BaseCheckpointSaver[str],
) -> Any:
    builder = StateGraph(cast(Any, SessionState))
    builder.add_node("secure_entry", _entry_node)
    builder.add_edge(START, "secure_entry")
    builder.add_edge("secure_entry", END)
    return builder.compile(checkpointer=checkpointer)


def _new_refinement_state() -> dict[str, object]:
    return {
        "version": REFINEMENT_VERSION,
        "failed_rounds": 0,
        "pending_refinement": False,
        "rejected_options": [],
        "considered_options": [],
        "intent_revisions": [],
    }


def _restore_refinement_state(value: object) -> dict[str, object]:
    if type(value) is not dict:
        return _new_refinement_state()
    raw = cast(dict[str, object], value)
    if (
        raw.get("version") != REFINEMENT_VERSION
        or type(raw.get("failed_rounds")) is not int
        or cast(int, raw["failed_rounds"]) < 0
        or type(raw.get("pending_refinement")) is not bool
        or type(raw.get("rejected_options")) is not list
        or type(raw.get("considered_options")) is not list
        or type(raw.get("intent_revisions")) is not list
    ):
        return _new_refinement_state()
    return {
        "version": REFINEMENT_VERSION,
        "failed_rounds": raw["failed_rounds"],
        "pending_refinement": raw["pending_refinement"],
        "rejected_options": list(cast(list[object], raw["rejected_options"])),
        "considered_options": list(cast(list[object], raw["considered_options"])),
        "intent_revisions": list(cast(list[object], raw["intent_revisions"])),
    }


def _previous_options(context: object) -> list[dict[str, str]] | None:
    plan = RecommendationPlan.from_dict(context)
    if plan is None or not plan.presented_game_ids:
        return None
    candidates = {candidate.game_id: candidate for candidate in plan.candidates}
    presented_ids = set(plan.presented_game_ids)
    result: list[dict[str, str]] = []
    for ranked in plan.ranking:
        if ranked.game_id not in presented_ids:
            continue
        candidate = candidates.get(ranked.game_id)
        if candidate is not None:
            result.append(
                {
                    "game_id": str(candidate.game_id),
                    "title": redact_sensitive_text(candidate.title),
                }
            )
    return result or None


def _changed_fields(previous: Intent, current: Intent) -> list[str]:
    return [
        key
        for key, value in current.to_dict().items()
        if previous.to_dict().get(key) != value
    ]


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
        demand_service: DemandService | None = None,
        max_message_age_seconds: int = 900,
        max_failed_refinement_rounds: int = 3,
    ) -> None:
        self.store = store
        self.context_references = context_references
        self.checkpointer_factory = checkpointer_factory
        self.intent_extraction = intent_extraction
        self.recommendations = recommendations
        self.handoff_service = handoff_service
        self.demand_service = demand_service
        self.max_message_age_seconds = max_message_age_seconds
        self.max_failed_refinement_rounds = max_failed_refinement_rounds

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
                "/start [context parameter redacted]" if has_parameter else "/start"
            )
        elif _HUMAN_COMMAND.fullmatch(message.text):
            command = "handoff"
            safe_text = "/humano"
        elif DemandService.is_command(message.text):
            command = "demand"
            safe_text = "[comando de demanda; conteúdo não retido]"
        else:
            safe_text = "[conteúdo de mensagem não retido]"
        if context_reference:
            try:
                context_game_id = self.context_references.validate(
                    context_reference
                ).game_id
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
                return ProcessingResult(
                    claim.status, claim.session_id, claim.reply_text
                )
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
                "demand_reply": None,
                "demand_draft": None,
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
                pending_demand = _restored_demand_draft(prior_state.get("demand_draft"))
                if command != "demand":
                    initial_state["demand_draft"] = pending_demand
                prior_intent = Intent.from_dict(prior_state.get("intent")) or Intent()
                # Rewriting the safe canonical form also replaces legacy checkpoints
                # whose individual fields contained sensitive text.
                initial_state["prepared_intent"] = prior_intent.to_dict()
                refinement = _restore_refinement_state(
                    prior_state.get("refinement_state")
                )
                prior_recommendation = self.store.latest_delivered_recommendation(
                    claim.session_id
                )
                previous_options = _previous_options(prior_recommendation)
                initial_state["last_recommendation_context"] = prior_recommendation
                if not already_processed and command == "demand":
                    initial_state["demand_reply"] = (
                        self.demand_service.handle(message, claim.session_id)
                        if self.demand_service
                        else "O registro de interesse está indisponível agora. Tente novamente mais tarde."
                    )
                elif not already_processed and command == "handoff":
                    if isinstance(prior_recommendation, dict):
                        self._record_considered_options(
                            refinement, prior_recommendation
                        )
                    initial_state["prepared_refinement_state"] = refinement
                    handoff = (
                        self.handoff_service.request(
                            session_id=claim.session_id,
                            channel=message.channel,
                            update_id=message.update_id,
                            correlation_id=message.correlation_id,
                            context_snapshot=self._handoff_context(
                                prior_intent, refinement, message.correlation_id
                            ),
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
                        previous_options=previous_options,
                    )
                    if (
                        decision.status == "accepted"
                        and self.demand_service is not None
                    ):
                        if decision.demand_action == "list":
                            initial_state["demand_draft"] = None
                            initial_state["demand_reply"] = self.demand_service.handle(
                                replace(message, text="/demandas"), claim.session_id
                            )
                        elif decision.demand_action == "cancel":
                            initial_state["demand_draft"] = None
                            demand_text = (
                                f"/cancelar_demanda {decision.demand_id or ''}"
                            )
                            initial_state["demand_reply"] = self.demand_service.handle(
                                replace(message, text=demand_text), claim.session_id
                            )
                        elif decision.demand_action == "register" or (
                            pending_demand is not None
                            and _is_demand_answer(
                                message.text,
                                decision.demand_title,
                                decision.intent.platform,
                            )
                        ):
                            draft = dict(pending_demand or {})
                            normalized_message = normalize(message.text)
                            if decision.demand_action == "register":
                                draft = {}
                            if (
                                decision.demand_title
                                and normalize(decision.demand_title)
                                in normalized_message
                            ):
                                draft["title"] = clean_description(
                                    decision.demand_title
                                )
                            if (
                                decision.intent.platform
                                and normalize(decision.intent.platform)
                                in normalized_message
                            ):
                                draft["platform"] = clean_description(
                                    decision.intent.platform, maximum=48
                                )
                            words = set(re.findall(r"[a-z]+", normalized_message))
                            expressed_modes = {
                                _DEMAND_MODE_ALIASES[word]
                                for word in words
                                if word in _DEMAND_MODE_ALIASES
                            }
                            explicit_mode = (
                                next(iter(expressed_modes))
                                if len(expressed_modes) == 1
                                else None
                            )
                            if explicit_mode:
                                draft["mode"] = explicit_mode
                            responds_to_demand = (
                                decision.demand_action == "register"
                                or draft != pending_demand
                                or (bool(expressed_modes) and not draft.get("mode"))
                            )
                            if responds_to_demand and all(
                                draft.get(field)
                                for field in ("title", "platform", "mode")
                            ):
                                demand_text = (
                                    f"/demanda {draft['title']} | {draft['platform']} | "
                                    f"{'compra' if draft['mode'] == 'purchase' else 'aluguel'}"
                                )
                                initial_state["demand_reply"] = (
                                    self.demand_service.handle(
                                        replace(message, text=demand_text),
                                        claim.session_id,
                                    )
                                )
                                initial_state["demand_draft"] = None
                            elif responds_to_demand:
                                initial_state["demand_reply"] = _demand_clarification(
                                    draft
                                )
                                initial_state["demand_draft"] = draft
                    allowed_rejections = {
                        item["game_id"] for item in previous_options or []
                    }
                    invalid_rejection = any(
                        str(item.game_id) not in allowed_rejections
                        for item in decision.rejections
                    )
                    if invalid_rejection or (
                        (decision.rejections or decision.rejection_ambiguous)
                        and not previous_options
                    ):
                        decision = replace(
                            decision,
                            status="fallback",
                            intent=prior_intent,
                            rejections=(),
                            rejection_ambiguous=False,
                        )
                    initial_state["prepared_intent"] = decision.intent.to_dict()
                    initial_state["prepared_provenance"] = decision.provenance.to_dict()
                    initial_state["extraction_status"] = decision.status
                    price_rejection_needs_range = (
                        decision.status == "accepted"
                        and any(item.reason == "price" for item in decision.rejections)
                        and not decision.price_range_updated
                    )
                    initial_state["clarification_field"] = (
                        "price_range"
                        if price_rejection_needs_range
                        else decision.clarification_field
                    )
                    initial_state["force_clarification"] = price_rejection_needs_range
                    initial_state["rejection_ambiguous"] = decision.rejection_ambiguous
                    current_refinement = _restore_refinement_state(refinement)
                    if isinstance(prior_recommendation, dict):
                        self._record_considered_options(
                            current_refinement, prior_recommendation
                        )
                    changed = _changed_fields(prior_intent, decision.intent)
                    if changed:
                        revisions = cast(
                            list[object], current_refinement["intent_revisions"]
                        )
                        revisions.append(
                            {
                                "revision": len(revisions) + 1,
                                "intent_version": INTENT_VERSION,
                                "intent": decision.intent.to_dict(),
                                "changed_fields": changed,
                                "source_update_id": message.update_id,
                                "correlation_id": str(message.correlation_id),
                                "provenance": decision.provenance.to_dict(),
                            }
                        )
                    presented = {
                        item["game_id"]: item["title"]
                        for item in previous_options or []
                    }
                    prior_plan = RecommendationPlan.from_dict(prior_recommendation)
                    recommendation_update_id = (
                        prior_plan.provenance.source_update_id
                        if prior_plan is not None
                        else message.update_id
                    )
                    rejected_history = cast(
                        list[object], current_refinement["rejected_options"]
                    )
                    for rejection in decision.rejections:
                        rejected_history.append(
                            {
                                "game_id": str(rejection.game_id),
                                "title": presented[str(rejection.game_id)],
                                "reason": rejection.reason,
                                "source_update_id": message.update_id,
                                "recommendation_update_id": recommendation_update_id,
                                "correlation_id": str(message.correlation_id),
                            }
                        )
                    if decision.rejections:
                        current_refinement["pending_refinement"] = True
                    initial_state["prepared_refinement_state"] = current_refinement
                    if (
                        decision.status == "accepted"
                        and not decision.rejection_ambiguous
                        and self.recommendations is not None
                        and not initial_state.get("demand_reply")
                    ):
                        current_intent = decision.intent
                        clarification = clarification_question(
                            cast(Any, initial_state["clarification_field"]),
                            current_intent,
                            force=price_rejection_needs_range,
                        )
                        if clarification is None:
                            outcome = self.recommendations.recommend(
                                current_intent,
                                session_id=claim.session_id,
                                channel=message.channel,
                                update_id=message.update_id,
                                correlation_id=message.correlation_id,
                                excluded_game_ids=self._excluded_ids(
                                    current_refinement
                                ),
                                previous_recommendation=prior_recommendation,
                            )
                            is_refinement_failure = bool(
                                current_refinement["pending_refinement"]
                            ) and (
                                outcome.status == "empty"
                                or bool(
                                    isinstance(outcome.context, dict)
                                    and outcome.context.get("reused_previous_options")
                                    is True
                                )
                            )
                            if is_refinement_failure:
                                current_refinement["failed_rounds"] = (
                                    cast(int, current_refinement["failed_rounds"]) + 1
                                )
                                current_refinement["pending_refinement"] = False
                            elif outcome.status == "accepted":
                                current_refinement["failed_rounds"] = 0
                                current_refinement["pending_refinement"] = False
                            outcome_recommendation = outcome
                            if (
                                is_refinement_failure
                                and cast(int, current_refinement["failed_rounds"])
                                >= self.max_failed_refinement_rounds
                            ):
                                handoff_context = self._handoff_context(
                                    decision.intent,
                                    current_refinement,
                                    message.correlation_id,
                                )
                                handoff = (
                                    self.handoff_service.request(
                                        session_id=claim.session_id,
                                        channel=message.channel,
                                        update_id=message.update_id,
                                        correlation_id=message.correlation_id,
                                        context_snapshot=handoff_context,
                                    )
                                    if self.handoff_service is not None
                                    else None
                                )
                                if (
                                    handoff is not None
                                    and handoff.status == "registered"
                                ):
                                    handoff_reply = (
                                        "Não encontrei uma alternativa adequada sem repetir as opções já vistas. "
                                        "Registrei o contexto para revisão humana no Sandbox; você não precisa "
                                        "repetir o que já informou."
                                    )
                                else:
                                    handoff_reply = (
                                        "Não encontrei uma alternativa adequada sem repetir as opções já vistas. "
                                        "Não consegui registrar a revisão humana agora. Você pode tentar /humano."
                                    )
                                outcome_recommendation = RecommendationOutcome(
                                    "empty", handoff_reply, None
                                )
                            if (
                                outcome_recommendation.status == "accepted"
                                and isinstance(outcome_recommendation.context, dict)
                            ):
                                self._record_considered_options(
                                    current_refinement,
                                    outcome_recommendation.context,
                                )
                            initial_state["prepared_refinement_state"] = (
                                current_refinement
                            )
                            if (
                                outcome_recommendation.status == "accepted"
                                and isinstance(outcome_recommendation.context, dict)
                            ):
                                outcome_recommendation.context[
                                    "greeting_required"
                                ] = not bool(prior_state.get("greeting_sent"))
                            initial_state["prepared_recommendation"] = {
                                "status": outcome_recommendation.status,
                                "reply_text": outcome_recommendation.reply_text,
                                "context": outcome_recommendation.context,
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

    @staticmethod
    def _record_considered_options(
        refinement: dict[str, object], context: dict[str, object]
    ) -> None:
        plan = RecommendationPlan.from_dict(context)
        if plan is None or not plan.presented_game_ids:
            return
        considered = cast(list[object], refinement["considered_options"])
        candidates = {item.game_id: item for item in plan.candidates}
        presented_ids = set(plan.presented_game_ids)
        for ranked in plan.ranking:
            if ranked.game_id not in presented_ids:
                continue
            candidate = candidates.get(ranked.game_id)
            if candidate is None:
                continue
            game_id = str(candidate.game_id)
            considered[:] = [
                item
                for item in considered
                if not (type(item) is dict and item.get("game_id") == game_id)
            ]
            considered.append(
                {
                    "game_id": game_id,
                    "title": redact_sensitive_text(candidate.title),
                }
            )
            if len(considered) > 100:
                del considered[:-100]

    @staticmethod
    def _excluded_ids(refinement: dict[str, object]) -> tuple[UUID, ...]:
        result: list[UUID] = []
        seen: set[UUID] = set()
        for item in cast(list[object], refinement["rejected_options"]):
            if type(item) is not dict or not isinstance(item.get("game_id"), str):
                continue
            item = cast(dict[str, object], item)
            raw_game_id = cast(str, item["game_id"])
            try:
                game_id = UUID(raw_game_id)
            except ValueError, TypeError, AttributeError:
                continue
            if str(game_id) == item["game_id"] and game_id not in seen:
                result.append(game_id)
                seen.add(game_id)
        return tuple(result)

    @staticmethod
    def _handoff_context(
        intent: Intent,
        refinement: dict[str, object],
        correlation_id: UUID,
    ) -> dict[str, object]:
        considered = [
            cast(dict[str, object], item)
            for item in cast(list[object], refinement["considered_options"])
            if type(item) is dict
            and isinstance(item.get("game_id"), str)
            and isinstance(item.get("title"), str)
        ]
        rejected: list[dict[str, object]] = []
        for item in cast(list[object], refinement["rejected_options"]):
            if (
                type(item) is dict
                and isinstance(item.get("game_id"), str)
                and isinstance(item.get("title"), str)
                and isinstance(item.get("reason"), str)
                and type(item.get("source_update_id")) is int
                and type(item.get("recommendation_update_id")) is int
            ):
                item = cast(dict[str, object], item)
                rejected.append(
                    {
                        "game_id": item["game_id"],
                        "title": redact_sensitive_text(cast(str, item["title"])),
                        "reason": item["reason"],
                        "source_update_id": item["source_update_id"],
                        "recommendation_update_id": item["recommendation_update_id"],
                    }
                )
        return {
            "version": "handoff-context.v1",
            "correlation_id": str(correlation_id),
            "intent": intent.to_dict(),
            "options_considered": [
                {
                    "game_id": item["game_id"],
                    "title": redact_sensitive_text(cast(str, item["title"])),
                }
                for item in considered[-100:]
            ],
            "rejected_options": rejected[-100:],
        }

    def _extract(
        self,
        message: IncomingMessage,
        session_id: UUID,
        previous: Intent,
        *,
        previous_options: list[dict[str, str]] | None = None,
    ) -> IntentDecision:
        if self.intent_extraction is not None:
            if previous_options is None:
                return self.intent_extraction.extract(
                    message.text,
                    previous,
                    session_id=session_id,
                    channel=message.channel,
                    update_id=message.update_id,
                    correlation_id=message.correlation_id,
                )
            return self.intent_extraction.extract(
                message.text,
                previous,
                session_id=session_id,
                channel=message.channel,
                update_id=message.update_id,
                correlation_id=message.correlation_id,
                previous_options=previous_options,
            )
        status = (
            "suspected_injection" if is_prompt_injection(message.text) else "normal"
        )
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
