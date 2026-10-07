from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.modules.concierge.adapters.simulator import (
    InMemorySessionStore,
    SessionSimulator,
)
from app.modules.concierge.application.context_reference import ContextReferenceService
from app.modules.concierge.application.session_workflow import SessionWorkflow
from app.modules.concierge.domain.session import IncomingMessage


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
    assert store._messages[("simulator", 1)]["workflow_version"] == "2.1.v1"


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
