from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Literal
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.modules.concierge.adapters.simulator import (
    InMemorySessionStore,
    SessionSimulator,
)
from app.modules.concierge.application.context_reference import ContextReferenceService
from app.modules.concierge.application.handoff import (
    HandoffDecision,
    HandoffService,
    _validated_context,
)
from app.modules.concierge.application.intent_extraction import IntentDecision
from app.modules.concierge.application.session_workflow import (
    SessionWorkflow,
    build_session_graph,
)
from app.modules.concierge.domain.intent import Intent, IntentProvenance
from app.modules.concierge.domain.session import IncomingMessage
from app.modules.concierge.ports.recommendations import RecommendationOutcome


class Catalog:
    def __init__(self, game_id) -> None:  # type: ignore[no-untyped-def]
        self.game_id = game_id

    def get_game(self, game_id):  # type: ignore[no-untyped-def]
        return object() if game_id == self.game_id else None


def setup_workflow(game_id=None, *, secret="test-secret"):  # type: ignore[no-untyped-def]
    store = InMemorySessionStore()
    saver = InMemorySaver()

    @contextmanager
    def checkpointer_factory():
        yield saver

    references = ContextReferenceService(Catalog(game_id), secret, 1800)
    workflow = SessionWorkflow(store, references, checkpointer_factory)
    return store, saver, references, workflow


def incoming(
    update_id: int,
    message_id: int,
    text: str,
    *,
    sent_at: datetime | None = None,
    user_id: str = "12345",
    reference: str | None = None,
) -> IncomingMessage:
    now = sent_at or datetime.now(UTC)
    return IncomingMessage(
        channel="simulator",
        external_user_id=user_id,
        external_chat_id=user_id,
        update_id=update_id,
        message_id=message_id,
        text=text,
        sent_at=now,
        received_at=datetime.now(UTC),
        correlation_id=uuid4(),
        context_reference=reference,
    )


def test_session_simulator_uses_same_versioned_workflow_and_persists_context() -> None:
    game_id = uuid4()
    store, _, references, workflow = setup_workflow(game_id)
    token, _ = references.create_token(game_id)
    simulator = SessionSimulator(workflow)

    result = simulator.send("simulated-user", "/start", game_reference=token)

    session = next(iter(store._sessions.values()))
    assert result.status == "claimed"
    assert result.session_id == session["id"]
    assert session["context_game_id"] == game_id
    assert "Sandbox" in result.reply_text
    assert "game_id" not in result.reply_text
    assert store._messages[("simulator", 1)]["workflow_version"] == "2.5.v1"


@pytest.mark.parametrize("text", ["", "x" * 4097, "nul\x00byte", "bell\x07"])
def test_session_simulator_rejects_text_telegram_would_ignore(text: str) -> None:
    _, _, _, workflow = setup_workflow()
    simulator = SessionSimulator(workflow)

    with pytest.raises(ValueError, match="invalid_message_text"):
        simulator.send("simulated-user", text)


def test_checkpoint_resumes_session_without_repeating_initial_greeting() -> None:
    store, saver, references, workflow = setup_workflow()
    instant = datetime.now(UTC).replace(microsecond=0)
    first = workflow.handle(incoming(1, 1, "Olá", sent_at=instant))
    second_workflow = SessionWorkflow(store, references, lambda: null_context(saver))
    second = second_workflow.handle(
        incoming(2, 2, "Quero falar de jogos", sent_at=instant + timedelta(seconds=1))
    )

    assert first.session_id == second.session_id
    assert first.reply_text != second.reply_text
    assert "Oi! Sou Pixel" in first.reply_text
    assert "Recebi sua mensagem" in second.reply_text
    assert len(store._sessions) == 1


class SequenceExtraction:
    def __init__(self, decisions):  # type: ignore[no-untyped-def]
        self.decisions = iter(decisions)

    def extract(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return next(self.decisions)


class GreetingRecommendations:
    def __init__(self) -> None:
        self.calls = 0
        self.commerce_available = True

    def recommend(self, intent, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return RecommendationOutcome(
            "accepted", "Opções encontradas", {"greeting_required": False, "facts": []}
        )

    def revalidate_and_compose(self, context):  # type: ignore[no-untyped-def]
        status = "eligible" if self.commerce_available else "no_eligible_offer"
        context["revalidation"] = {"facts": [{"status": status}]}
        if not self.commerce_available:
            return "Não consegui confirmar opções comerciais agora. Você pode pesquisar o catálogo ou falar com uma pessoa usando /humano."
        prefix = "Oi! Sou Pixel, assistente de IA da RetroVault. Esta conversa de demonstração acontece no Sandbox. " if context["greeting_required"] else ""
        return prefix + "Opções revalidadas com fatos comerciais atuais."


def _intent_decision(status: str, clarification: str = "none") -> IntentDecision:
    return IntentDecision(
        status, Intent(genre="Adventure"), clarification,
        IntentProvenance(
            intent_version="intent.v2", prompt_version="intent-extraction.v2",
            workflow_version="2.3.v1", configuration_version="test",
            source_update_id=1, correlation_id=uuid4(),
            safety_classification="normal",
        ),
    )  # type: ignore[arg-type]


def test_clarification_after_recommendation_clears_prepared_outbox_context() -> None:
    store, saver, references, _ = setup_workflow()
    recommendations = GreetingRecommendations()
    extraction = SequenceExtraction([
        _intent_decision("accepted"), _intent_decision("accepted", "platform")
    ])
    workflow = SessionWorkflow(
        store, references, lambda: null_context(saver),
        intent_extraction=extraction, recommendations=recommendations,
    )
    now = datetime.now(UTC)
    first = workflow.handle(incoming(501, 501, "Quero aventura", sent_at=now))
    assert "Oi! Sou Pixel" in first.reply_text
    delivery = workflow.claim_reply(501, channel="simulator")
    assert delivery is not None
    assert "Oi! Sou Pixel" in delivery.text
    assert "Sandbox" in delivery.text
    assert store._messages[("simulator", 501)]["reply"] == delivery.text

    clarified = workflow.handle(incoming(
        502, 502, "Pode ser qualquer plataforma", sent_at=now + timedelta(seconds=1)
    ))
    assert "Em qual plataforma" in clarified.reply_text
    assert recommendations.calls == 1
    assert store._messages[("simulator", 502)]["recommendation_context"] is None


def test_fallback_and_handoff_after_recommendation_clear_old_context() -> None:
    store, saver, references, _ = setup_workflow()
    recommendations = GreetingRecommendations()
    workflow = SessionWorkflow(
        store, references, lambda: null_context(saver),
        intent_extraction=SequenceExtraction([
            _intent_decision("accepted"), _intent_decision("fallback")
        ]), recommendations=recommendations,
    )
    now = datetime.now(UTC)
    workflow.handle(incoming(601, 601, "Quero aventura", sent_at=now))
    fallback = workflow.handle(incoming(602, 602, "mensagem ilegível", sent_at=now + timedelta(seconds=1)))
    handoff = workflow.handle(incoming(603, 603, "/humano", sent_at=now + timedelta(seconds=2)))
    assert "não consegui interpretá-la" in fallback.reply_text.casefold()
    assert "encaminhamento humano" in handoff.reply_text.casefold()
    assert store._messages[("simulator", 602)]["recommendation_context"] is None
    assert store._messages[("simulator", 603)]["recommendation_context"] is None


@pytest.mark.parametrize(
    ("handoff_status", "expected_reply"),
    [
        ("registered", "Registrei o contexto para revisão humana"),
        ("unavailable", "Não consegui registrar a revisão humana agora"),
    ],
)
@pytest.mark.parametrize("recommendation_status", ["empty", "accepted"])
def test_automatic_handoff_at_refinement_limit_persists_reply_without_recommendation(
    monkeypatch: pytest.MonkeyPatch,
    handoff_status: Literal["registered", "unavailable"],
    expected_reply: str,
    recommendation_status: Literal["empty", "accepted"],
) -> None:
    store, saver, references, _ = setup_workflow()
    handoff_service = HandoffService(None, frozenset())
    handoff_calls: list[dict[str, object]] = []

    def request(**kwargs: object) -> HandoffDecision:
        handoff_calls.append(kwargs)
        return HandoffDecision(handoff_status)

    recommendations = GreetingRecommendations()
    monkeypatch.setattr(handoff_service, "request", request)
    monkeypatch.setattr(
        recommendations,
        "recommend",
        lambda *args, **kwargs: RecommendationOutcome(
            recommendation_status, "Sem alternativas", {"reused_previous_options": True}
        ),
    )
    workflow = SessionWorkflow(
        store, references, lambda: null_context(saver),
        intent_extraction=SequenceExtraction([_intent_decision("accepted")]),
        recommendations=recommendations,
        handoff_service=handoff_service,
        max_failed_refinement_rounds=3,
    )
    now = datetime.now(UTC)
    started = workflow.handle(incoming(651, 651, "/start", sent_at=now))
    config = {"configurable": {"thread_id": str(started.session_id)}}
    graph = build_session_graph(saver)
    game_id = str(uuid4())
    considered = [{"game_id": game_id, "title": "Adventure Quest"}]
    rejected = [{
        "game_id": game_id,
        "title": "Adventure Quest",
        "reason": "style",
        "source_update_id": 650,
        "recommendation_update_id": 649,
    }]
    graph.update_state(config, {"refinement_state": {
        "version": "refinement.v1",
        "failed_rounds": 2,
        "pending_refinement": True,
        "rejected_options": rejected,
        "considered_options": considered,
        "intent_revisions": [],
    }})

    event = incoming(652, 652, "Quero outra alternativa", sent_at=now + timedelta(seconds=1))
    result = workflow.handle(event)

    assert result.status == "claimed"
    assert expected_reply in result.reply_text
    persisted = store._messages[("simulator", 652)]
    assert persisted["reply"] == result.reply_text
    assert persisted["recommendation_context"] is None
    assert len(handoff_calls) == 1
    assert handoff_calls[0]["session_id"] == started.session_id
    assert handoff_calls[0]["update_id"] == 652
    snapshot = handoff_calls[0]["context_snapshot"]
    assert snapshot == {
        "version": "handoff-context.v1",
        "correlation_id": str(event.correlation_id),
        "intent": Intent(genre="Adventure").to_dict(),
        "options_considered": considered,
        "rejected_options": rejected,
    }
    assert _validated_context(snapshot, event.correlation_id) == snapshot
    state = graph.get_state(config).values["refinement_state"]
    assert state["failed_rounds"] == 3
    assert state["pending_refinement"] is False


def test_immediate_outbox_redrive_persists_fresh_commerce_evidence_and_text() -> None:
    store, saver, references, _ = setup_workflow()
    recommendations = GreetingRecommendations()
    workflow = SessionWorkflow(
        store, references, lambda: null_context(saver),
        intent_extraction=SequenceExtraction([_intent_decision("accepted")]),
        recommendations=recommendations,
    )
    event = incoming(701, 701, "Quero aventura")
    workflow.handle(event)
    recommendations.commerce_available = False
    delivery = workflow.claim_reply(event.update_id, channel=event.channel)
    assert delivery is not None
    assert "não consegui confirmar opções comerciais" in delivery.text.casefold()
    persisted = store._messages[(event.channel, event.update_id)]
    assert persisted["reply"] == delivery.text
    assert persisted["recommendation_context"]["revalidation"]["facts"][0]["status"] == "no_eligible_offer"


def test_pending_replies_exclude_lost_lease_and_keep_other_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, saver, references, _ = setup_workflow()
    workflow = SessionWorkflow(
        store, references, lambda: null_context(saver),
        intent_extraction=SequenceExtraction([
            _intent_decision("accepted"), _intent_decision("accepted")
        ]),
        recommendations=GreetingRecommendations(),
    )
    for update_id in (801, 802):
        workflow.handle(replace(
            incoming(update_id, update_id, "Quero aventura", user_id=str(update_id)),
            channel="telegram",
        ))

    original_update = store.update_outbox_recommendation

    def update_with_lost_lease(update_id: int, **kwargs):  # type: ignore[no-untyped-def]
        if update_id == 801:
            store._messages[("telegram", update_id)]["delivery_lease_token"] = uuid4()
        return original_update(update_id, **kwargs)

    monkeypatch.setattr(store, "update_outbox_recommendation", update_with_lost_lease)

    replies = workflow.claim_pending_replies()

    assert [reply.update_id for reply in replies] == [802]
    assert "Opções revalidadas" in replies[0].text


def test_valid_contextual_start_attaches_game_to_existing_generic_session() -> None:
    game_id = uuid4()
    store, saver, references, workflow = setup_workflow(game_id)
    instant = datetime.now(UTC).replace(microsecond=0)
    first = workflow.handle(incoming(1, 1, "Olá", sent_at=instant))
    reference, _ = references.create_token(game_id)
    contextual = workflow.handle(
        incoming(
            2,
            2,
            f"/start {reference}",
            sent_at=instant + timedelta(seconds=1),
            reference=reference,
        )
    )

    session = next(iter(store._sessions.values()))
    checkpoint = saver.get_tuple({"configurable": {"thread_id": str(first.session_id)}})

    assert contextual.session_id == first.session_id
    assert session["context_game_id"] == game_id
    assert checkpoint is not None
    assert checkpoint.checkpoint["channel_values"]["context_game_id"] == str(game_id)


@contextmanager
def null_context(value):  # type: ignore[no-untyped-def]
    yield value


def test_stale_start_messages_are_not_attached_and_are_redacted_from_persistence() -> None:
    game_id = uuid4()
    store, _, references, workflow = setup_workflow(game_id)
    token, _ = references.create_token(game_id)
    expired = incoming(1, 1, f"/start {token}")
    expired = IncomingMessage(
        **{
            **expired.__dict__,
            "sent_at": datetime.now(UTC) - timedelta(minutes=16),
        }
    )

    result = workflow.handle(expired)

    assert result.status == "reconciliation"
    assert store._sessions == {}
    assert store._messages[("simulator", 1)]["safe_text"] == "/start [context reference redacted]"


def test_missing_signing_secret_continues_without_unverified_context() -> None:
    game_id = uuid4()
    store, saver, _, _ = setup_workflow(game_id)
    signed_references = ContextReferenceService(Catalog(game_id), "signing-secret", 1800)
    token, _ = signed_references.create_token(game_id)
    workflow = SessionWorkflow(
        store,
        ContextReferenceService(Catalog(game_id), ""),
        lambda: null_context(saver),
    )

    result = workflow.handle(incoming(3, 3, f"/start {token}", reference=token))

    record = store._messages[("simulator", 3)]
    assert result.status == "claimed"
    assert store._sessions[("simulator", "12345")]["context_game_id"] is None
    assert record["safe_text"] == "/start [context reference redacted]"
    assert "não pôde ser validado" in result.reply_text


def test_malformed_start_parameter_is_redacted_before_persistence() -> None:
    store, _, _, workflow = setup_workflow()
    raw_parameter = "segredo-nao-persistir!"

    result = workflow.handle(incoming(4, 4, f"/start {raw_parameter}"))

    record = store._messages[("simulator", 4)]
    assert result.status == "claimed"
    assert record["safe_text"] == "/start [context parameter redacted]"
    assert raw_parameter not in record["safe_text"]


def test_simulator_session_processing_lock_serializes_graph_work_per_session() -> None:
    store, _, _, workflow = setup_workflow()
    first = workflow.handle(incoming(5, 5, "Primeira mensagem"))
    assert first.session_id is not None
    attempting = Event()
    entered = Event()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.session_processing_lock(first.session_id):
            future = pool.submit(
                lambda: _enter_session_lock(
                    store, first.session_id, attempting, entered
                )
            )
            assert attempting.wait(timeout=1)
            assert not entered.is_set()
    future.result(timeout=1)
    assert entered.is_set()


def _enter_session_lock(store, session_id, attempting, entered) -> None:  # type: ignore[no-untyped-def]
    attempting.set()
    with store.session_processing_lock(session_id):
        entered.set()


def test_out_of_order_message_is_reconciliation_only_and_does_not_rewind_state() -> None:
    store, _, _, workflow = setup_workflow()
    instant = datetime.now(UTC).replace(microsecond=0)
    first = workflow.handle(incoming(10, 10, "Primeira", sent_at=instant))
    delayed = workflow.handle(incoming(9, 9, "Antiga", sent_at=instant))
    follow_up = workflow.handle(
        incoming(11, 11, "Atual", sent_at=instant + timedelta(seconds=1))
    )

    assert delayed.status == "reconciliation"
    assert delayed.reply_text is None
    assert follow_up.session_id == first.session_id
    assert "Recebi sua mensagem" in follow_up.reply_text
    assert store._messages[("simulator", 9)]["status"] == "reconciliation"


def test_concurrent_duplicate_claims_are_serialized_by_simulator_store() -> None:
    store, _, _, _ = setup_workflow()
    event = incoming(77, 77, "Uma mensagem")

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(
                lambda _: store.claim_message(
                    event,
                    safe_text=event.text,
                    context_game_id=None,
                    now=event.received_at,
                    max_age_seconds=900,
                ),
                range(8),
            )
        )

    assert sum(claim.status == "claimed" for claim in claims) == 1
    assert sum(claim.status == "in_progress" for claim in claims) == 7
    assert len(store._sessions) == 1


def test_expired_processing_lease_fences_the_previous_worker() -> None:
    store, _, _, _ = setup_workflow()
    event = incoming(88, 88, "Mensagem retomada")
    first_claim = store.claim_message(
        event,
        safe_text=event.text,
        context_game_id=None,
        now=event.received_at,
        max_age_seconds=900,
    )
    assert first_claim.session_id is not None
    assert first_claim.processing_lease_token is not None

    reclaimed = store.claim_message(
        event,
        safe_text=event.text,
        context_game_id=None,
        now=event.received_at + timedelta(minutes=3),
        max_age_seconds=900,
    )
    assert reclaimed.status == "claimed"
    assert reclaimed.processing_lease_token is not None
    assert reclaimed.processing_lease_token != first_claim.processing_lease_token
    assert not store.is_processing_lease_current(
        event.update_id,
        channel=event.channel,
        session_id=first_claim.session_id,
        processing_lease_token=first_claim.processing_lease_token,
        now=event.received_at + timedelta(minutes=3),
    )
    assert store.is_processing_lease_current(
        event.update_id,
        channel=event.channel,
        session_id=reclaimed.session_id,
        processing_lease_token=reclaimed.processing_lease_token,
        now=event.received_at + timedelta(minutes=3),
    )

    with pytest.raises(RuntimeError, match="session_processing_lease_lost"):
        store.complete_message(
            event.update_id,
            channel=event.channel,
            session_id=first_claim.session_id,
            processing_lease_token=first_claim.processing_lease_token,
            reply_text="Resposta antiga",
            workflow_version="2.1.v1",
        )

    assert store._messages[(event.channel, event.update_id)]["status"] == "processing"


def test_outbox_delivery_lease_serializes_retries_and_fences_old_worker() -> None:
    store, _, _, workflow = setup_workflow()
    event = incoming(89, 89, "Mensagem com resposta pendente")
    result = workflow.handle(event)
    assert result.status == "claimed"

    first_delivery = workflow.claim_reply(event.update_id, channel=event.channel)
    assert first_delivery is not None
    assert workflow.claim_reply(event.update_id, channel=event.channel) is None

    expired_at = datetime.now(UTC) + timedelta(seconds=31)
    reclaimed = store.claim_reply(
        event.update_id,
        channel=event.channel,
        now=expired_at,
    )
    assert reclaimed is not None
    assert reclaimed.lease_token != first_delivery.lease_token
    assert not store.mark_reply_delivered(
        event.update_id,
        channel=event.channel,
        lease_token=first_delivery.lease_token,
    )
    assert store.mark_reply_delivered(
        event.update_id,
        channel=event.channel,
        lease_token=reclaimed.lease_token,
    )
    assert store.claim_reply(
        event.update_id,
        channel=event.channel,
        now=datetime.now(UTC),
    ) is None


def test_failed_outbox_delivery_is_deferred_before_redrive() -> None:
    store, _, _, workflow = setup_workflow()
    event = incoming(90, 90, "Retry com atraso")
    assert workflow.handle(event).status == "claimed"
    delivery = workflow.claim_reply(event.update_id, channel=event.channel)
    assert delivery is not None

    assert workflow.release_reply(
        event.update_id,
        channel=event.channel,
        lease_token=delivery.lease_token,
    )
    assert store.claim_reply(
        event.update_id, channel=event.channel, now=datetime.now(UTC)
    ) is None
    retried = store.claim_reply(
        event.update_id,
        channel=event.channel,
        now=datetime.now(UTC) + timedelta(seconds=31),
    )
    assert retried is not None


def test_distinct_updates_wait_for_the_active_checkpoint_transition() -> None:
    store, _, _, _ = setup_workflow()
    first = incoming(101, 101, "Primeira mensagem")
    first_claim = store.claim_message(
        first,
        safe_text=first.text,
        context_game_id=None,
        now=first.received_at,
        max_age_seconds=900,
    )
    next_message = incoming(
        102,
        102,
        "Segunda mensagem",
        sent_at=first.sent_at + timedelta(seconds=1),
    )

    blocked = store.claim_message(
        next_message,
        safe_text=next_message.text,
        context_game_id=None,
        now=next_message.received_at,
        max_age_seconds=900,
    )
    assert blocked.status == "in_progress"
    assert ("simulator", next_message.update_id) not in store._messages
    assert first_claim.session_id is not None
    assert first_claim.processing_lease_token is not None
    store.complete_message(
        first.update_id,
        channel=first.channel,
        session_id=first_claim.session_id,
        processing_lease_token=first_claim.processing_lease_token,
        reply_text="Resposta inicial",
        workflow_version="2.1.v1",
    )
    retried = store.claim_message(
        next_message,
        safe_text=next_message.text,
        context_game_id=None,
        now=next_message.received_at,
        max_age_seconds=900,
    )

    assert retried.status == "claimed"


def test_new_message_after_terminal_session_starts_a_new_session() -> None:
    store, _, _, workflow = setup_workflow()
    instant = datetime.now(UTC).replace(microsecond=0)
    first = workflow.handle(incoming(201, 201, "Olá", sent_at=instant))
    terminal_session = store._sessions[("simulator", "12345")]
    terminal_session["status"] = "terminal"

    next_session = workflow.handle(
        incoming(202, 202, "Olá novamente", sent_at=instant + timedelta(seconds=1))
    )

    assert next_session.status == "claimed"
    assert next_session.session_id != first.session_id
    assert terminal_session["status"] == "terminal"
    assert next(iter(store._sessions.values()))["id"] == next_session.session_id
    assert first.session_id in store._terminal_sessions
