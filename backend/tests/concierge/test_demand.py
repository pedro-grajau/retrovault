"""Consent, deterministic matching, channel ownership and command regression."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.modules.catalog.ports.repository import CatalogReadUnavailable
from app.modules.commerce.ports.offers import CommerceReadUnavailable
from app.modules.concierge.adapters.simulator import (
    InMemorySessionStore,
    SessionSimulator,
)
from app.modules.concierge.application.context_reference import ContextReferenceService
from app.modules.concierge.application.demand import DemandService
from app.modules.concierge.application.session_workflow import SessionWorkflow
from app.modules.concierge.domain.demand import Demand, normalize
from app.modules.concierge.domain.session import IncomingMessage


class MemoryDemands:
    def __init__(self):
        self.rows = {}
        self.effects = {}
        self.lock = Lock()
        self.queued = set()
        self.sold = []

    def propose(self, **kwargs):
        key = (kwargs["owner_key"], kwargs["channel"], kwargs["update_id"])
        if key not in self.effects:
            demand = Demand(
                uuid4(),
                kwargs["owner_key"],
                kwargs["channel"],
                kwargs["title"],
                kwargs["platform"],
                kwargs["mode"],
                kwargs["game_id"],
                "proposed",
                datetime.now(UTC),
            )
            self.rows[demand.id] = demand
            self.effects[key] = demand.id
        return self.rows[self.effects[key]]

    def confirm(self, demand_id, *, owner_key, channel, update_id):
        with self.lock:
            d = self.rows.get(demand_id)
            if (
                d is None
                or d.owner_key != owner_key
                or d.channel != channel
                or d.status not in {"proposed", "active"}
            ):
                return None
            other = next(
                (
                    r
                    for r in self.rows.values()
                    if r.status == "active"
                    and (
                        r.owner_key,
                        r.channel,
                        normalize(r.title),
                        normalize(r.platform),
                        r.mode,
                    )
                    == (
                        d.owner_key,
                        d.channel,
                        normalize(d.title),
                        normalize(d.platform),
                        d.mode,
                    )
                ),
                None,
            )
            if other:
                return other
            self.rows[d.id] = replace(
                d, status="active", consented_at=datetime.now(UTC)
            )
            return self.rows[d.id]

    def get_owned(self, demand_id, *, owner_key, channel):
        d = self.rows.get(demand_id)
        return d if d and d.owner_key == owner_key and d.channel == channel else None

    def list_owned(self, *, owner_key, channel):
        return [
            d
            for d in self.rows.values()
            if d.owner_key == owner_key
            and d.channel == channel
            and d.status in {"proposed", "active"}
        ]

    def cancel(self, demand_id, *, owner_key, channel):
        d = self.rows.get(demand_id)
        if d is None or d.owner_key != owner_key or d.channel != channel:
            return False
        if d.status in {"proposed", "active"}:
            self.rows[d.id] = replace(d, status="cancelled")
        return True

    def active(self):
        return [d for d in self.rows.values() if d.status == "active"]

    def enqueue(self, demand_id, *, event_id, game_id):
        self.queued.add((demand_id, event_id))
        self.rows[demand_id] = replace(self.rows[demand_id], game_id=game_id)

    def close_sold(self, demand_id, *, event_id):
        self.rows[demand_id] = replace(self.rows[demand_id], status="sold")
        self.sold.append((demand_id, event_id))


class Catalog:
    def __init__(self):
        self.games = []
        self.failed = False

    def search_games(self, **kwargs):
        if self.failed:
            raise CatalogReadUnavailable
        return [SimpleNamespace(game=g) for g in self.games], None

    def list_games(self, **kwargs):
        if self.failed:
            raise CatalogReadUnavailable
        return self.games, None

    def get_game(self, game_id):
        if self.failed:
            raise CatalogReadUnavailable
        return next((g for g in self.games if g.id == game_id), None)


class Offers:
    def __init__(self):
        self.units = 0
        self.failed = False

    def list_offers(self, game_ids):
        if self.failed:
            raise CommerceReadUnavailable
        return {
            g: [
                SimpleNamespace(
                    mode="purchase",
                    available_units=self.units,
                    price_minor=4990,
                    condition_summary="Boa",
                )
            ]
            for g in game_ids
        }


def message(text, update=1, user="owner", channel="simulator"):
    now = datetime.now(UTC)
    return IncomingMessage(channel, user, user, update, update, text, now, now, uuid4())


def setup():
    store, catalog, offers = MemoryDemands(), Catalog(), Offers()
    return DemandService(store, catalog, offers)


def register(service, user="owner"):
    service.handle(
        message("/demanda Chrono Trigger | SNES | compra", user=user), uuid4()
    )
    d = next(
        d
        for d in service.store.rows.values()
        if d.owner_key == service.owner(message("", user=user))
    )
    return service.store.confirm(
        d.id, owner_key=d.owner_key, channel=d.channel, update_id=2
    )


def test_consent_separate_from_proposal_and_portuguese_mode():
    service = setup()
    reply = service.handle(message("/demanda Chrono Trigger | SNES | compra"), uuid4())
    d = next(iter(service.store.rows.values()))
    assert d.status == "proposed"
    assert d.game_id is None
    assert "reserva" in reply and "prioridade" in reply and "prazo" in reply
    assert "purchase" not in reply and "compra" in reply
    assert "/confirmar_demanda" in reply
    service.handle(message(f"/confirmar_demanda {d.id}", 2), uuid4())
    assert service.store.rows[d.id].status == "active"
    assert service.store.rows[d.id].consented_at.tzinfo is UTC


def test_only_owner_can_cancel_and_repeat_cancellation_is_factual():
    service = setup()
    d = register(service)
    reply = service.handle(
        message(f"/cancelar_demanda {d.id}", 3, user="intruder"), uuid4()
    )
    assert "não encontrado" in reply and d.title not in reply
    assert service.store.rows[d.id].status == "active"
    for update in (4, 5):
        expected = "Interesse encerrado" if update == 4 else "já estava encerrado"
        assert expected in service.handle(
            message(f"/cancelar_demanda {d.id}", update), uuid4()
        )


def test_repeated_update_preserves_effect_and_cross_session_dedup():
    service = setup()
    for _ in range(2):
        service.handle(message("/demanda Chrono Trigger | SNES | compra"), uuid4())
    assert len(service.store.rows) == 1
    d = register(service)
    service.handle(message("/demanda chrono trigger | snes | compra", 10), uuid4())
    proposal = max(service.store.rows.values(), key=lambda r: r.created_at)
    service.handle(message(f"/confirmar_demanda {proposal.id}", 11), uuid4())
    assert len(service.store.active()) == 1 and service.store.active()[0].id == d.id


def test_commerce_failure_never_treated_as_empty_stock():
    service = setup()
    service.catalog.games = [
        SimpleNamespace(id=uuid4(), title="Chrono Trigger", platform="SNES")
    ]
    service.offers.failed = True
    reply = service.handle(message("/demanda Chrono Trigger | SNES | compra"), uuid4())
    assert "não consegui" in reply.lower() and service.store.rows == {}


def test_absent_preserves_sanitized_description_and_ambiguous_never_binds():
    service = setup()
    service.handle(
        message("/demanda Game email person@example.com | SNES | aluguel"), uuid4()
    )
    d = next(iter(service.store.rows.values()))
    assert "person@example.com" not in d.title and d.game_id is None
    service.catalog.games = [
        SimpleNamespace(id=uuid4(), title="Chrono Trigger", platform="SNES")
        for _ in range(2)
    ]
    assert "distinção" in service.handle(
        message("/demanda Chrono Trigger | SNES | compra", 2), uuid4()
    )
    assert len(service.store.rows) == 1


def test_available_title_does_not_register():
    service = setup()
    service.offers.units = 1
    service.catalog.games = [
        SimpleNamespace(id=uuid4(), title="Chrono Trigger", platform="SNES")
    ]
    assert "já tem disponibilidade" in service.handle(
        message("/demanda Chrono Trigger | SNES | compra"), uuid4()
    )
    assert not service.store.rows


def test_workflow_commands_available_without_ai_and_preserve_refinements():
    service = setup()
    sessions = InMemorySessionStore()
    saver = InMemorySaver()

    @contextmanager
    def checkpoints():
        yield saver

    workflow = SessionWorkflow(
        sessions,
        ContextReferenceService(service.catalog, "test", 1800),
        checkpoints,
        demand_service=service,
    )
    simulator = SessionSimulator(workflow)
    first = simulator.send("owner", "/demanda Chrono Trigger | SNES | compra")
    d = next(iter(service.store.rows.values()))
    assert "/confirmar_demanda" in first.reply_text
    second = simulator.send("owner", f"/confirmar_demanda {d.id}")
    assert "registrado" in second.reply_text and "purchase" not in second.reply_text
    assert service.store.rows[d.id].status == "active"
    assert "Chrono Trigger" in simulator.send("owner", "/demandas").reply_text
    assert (
        "encerrado" in simulator.send("owner", f"/cancelar_demanda {d.id}").reply_text
    )


def test_published_title_without_units_offers_consent_and_keeps_game_context():
    service = setup()
    game = SimpleNamespace(id=uuid4(), title="Chrono Trigger", platform="SNES")
    service.catalog.games = [game]
    reply = service.handle(message("/demanda Chrono Trigger | SNES | compra"), uuid4())
    demand = next(iter(service.store.rows.values()))
    assert demand.game_id == game.id and demand.status == "proposed"
    assert "/confirmar_demanda" in reply
    service.handle(message(f"/confirmar_demanda {demand.id}", 2), uuid4())
    assert service.store.rows[demand.id].status == "active"


def test_long_listing_keeps_complete_cancellation_codes_and_factual_truncation():
    service = setup()
    for update in range(1, 110):
        service.handle(
            message(f"/demanda {'L' * 200} {update} | SNES | compra", update), uuid4()
        )
    reply = service.handle(message("/demandas", 200), uuid4())
    assert len(reply) <= 3000
    assert "Lista abreviada" in reply and "código já recebido" in reply
    import re

    codes = re.findall(r"/cancelar_demanda ([0-9a-f-]+)", reply)
    assert codes and all(len(code) == 36 for code in codes)


def test_owned_confirmation_is_independent_of_display_pagination():
    service = setup()
    service.handle(message("/demanda Chrono Trigger | SNES | compra"), uuid4())
    old = next(iter(service.store.rows.values()))
    service.store.list_owned = lambda **kwargs: []
    reply = service.handle(message(f"/confirmar_demanda {old.id}", 2), uuid4())
    assert "registrado" in reply and service.store.rows[old.id].status == "active"


def test_title_boundaries_use_compatible_queries_and_exact_full_matching():
    service = setup()
    for length in (1, 2, 100, 101, 200):
        title = "X" * length
        game = SimpleNamespace(id=uuid4(), title=title, platform="SNES")
        service.catalog.games = [
            game,
            SimpleNamespace(id=uuid4(), title=title + "Y", platform="SNES"),
        ]
        assert service.resolve(title, "SNES") == game.id
    original = service.catalog.search_games

    def checked_search(**kwargs):
        assert 2 <= len(kwargs["query"]) <= 100
        return original(**kwargs)

    service.catalog.search_games = checked_search
    for length in (1, 101, 200):
        assert service.resolve("X" * length, "SNES") == (
            game.id if length == 200 else None
        )


def test_whitespace_commands_parse_tabs_and_newlines_consistently():
    service = setup()
    reply = service.handle(message("/demanda\tChrono Trigger | SNES | compra"), uuid4())
    d = next(iter(service.store.rows.values()))
    assert "/confirmar_demanda" in reply
    assert "registrado" in service.handle(
        message(f"/confirmar_demanda\n{d.id}", 2), uuid4()
    )
    assert "encerrado" in service.handle(
        message(f"/cancelar_demanda\t{d.id}", 3), uuid4()
    )


def test_restock_during_consent_is_reconciled_once_and_replay_revalidates():
    service = setup()
    game = SimpleNamespace(id=uuid4(), title="Chrono Trigger", platform="SNES")
    service.catalog.games = [game]
    service.handle(message("/demanda Chrono Trigger | SNES | compra"), uuid4())
    d = next(iter(service.store.rows.values()))
    original = service.store.confirm

    def restocked_confirm(*args, **kwargs):
        result = original(*args, **kwargs)
        service.offers.units = 1
        return result

    service.store.confirm = restocked_confirm
    for update in (2, 3):
        assert "registrado" in service.handle(
            message(f"/confirmar_demanda {d.id}", update), uuid4()
        )
    assert len(service.store.queued) == 1
    from app.modules.concierge.application.demand_notifications import (
        DemandNotificationService,
    )
    from app.modules.concierge.domain.demand import DemandNotification

    notice = DemandNotification(
        uuid4(),
        service.store.rows[d.id],
        "owner",
        next(iter(service.store.queued))[1],
        uuid4(),
        1,
    )
    service.offers.units = 0
    assert DemandNotificationService(service, None).prepare(notice) is None


def test_v4_natural_actions_use_budgeted_extraction_and_preserve_conversation():
    from tests.concierge.test_intent_extraction import (
        FakeGateway,
        FakeLedger,
        extraction_json,
    )
    from tests.concierge.test_intent_extraction import service as extraction_service

    service = setup()
    gateway, ledger = FakeGateway(), FakeLedger()
    sessions, saver = InMemorySessionStore(), InMemorySaver()

    @contextmanager
    def checkpoints():
        yield saver

    class Recommendations:
        def recommend(self, *args, **kwargs):
            raise AssertionError("Demand actions must suppress recommendation calls")

    workflow = SessionWorkflow(
        sessions,
        ContextReferenceService(service.catalog, "test", 1800),
        checkpoints,
        demand_service=service,
        intent_extraction=extraction_service(gateway, ledger),
        recommendations=Recommendations(),
    )
    simulator = SessionSimulator(workflow)

    def output(action, **extra):
        gateway.output_json = extraction_json(
            cleared_fields=[],
            rejections=[],
            rejection_ambiguous=False,
            demand_action=action,
            demand_title=None,
            demand_id=None,
            **extra,
        )

    output("register", platform="SNES", mode="purchase", genre="Aventura")
    # Set the literally expressed title on the strict provider output.
    import json

    payload = json.loads(gateway.output_json)
    payload["demand_title"] = "Chrono Trigger"
    gateway.output_json = json.dumps(payload)
    first = simulator.send(
        "owner", "Quero um aviso de Chrono Trigger para SNES, compra"
    )
    d = next(iter(service.store.rows.values()))
    assert "/confirmar_demanda" in first.reply_text and d.status == "proposed"
    # Natural-language registration is only a proposal; only this command consents.
    assert (
        "registrado" in simulator.send("owner", f"/confirmar_demanda {d.id}").reply_text
    )
    output("list")
    assert (
        "Chrono Trigger" in simulator.send("owner", "Liste meus interesses").reply_text
    )
    output("cancel")
    payload = json.loads(gateway.output_json)
    payload["demand_id"] = str(d.id)
    gateway.output_json = json.dumps(payload)
    assert (
        "encerrado"
        in simulator.send("owner", f"Cancele meu interesse {d.id}").reply_text
    )
    state = saver.get_tuple(
        {"configurable": {"thread_id": str(first.session_id)}}
    ).checkpoint["channel_values"]
    assert (
        state["intent"]["platform"] == "SNES" and state["intent"]["genre"] == "Aventura"
    )
    assert state["refinement_state"]["version"]
    assert ledger.reserve_calls == 3 and gateway.provider_calls == 3


@pytest.mark.parametrize("status", ["cancelled", "notified", "sold"])
def test_cancel_already_closed_interest_reports_original_state(status):
    service = setup()
    demand = register(service)
    service.store.rows[demand.id] = replace(demand, status=status)
    reply = service.handle(message(f"/cancelar_demanda {demand.id}", 3), uuid4())
    assert "já estava encerrado" in reply
    assert service.store.rows[demand.id].status == status
    assert "não encontrado" in service.handle(
        message(f"/cancelar_demanda {demand.id}", 4, user="other"), uuid4()
    )


def test_natural_demand_clarifies_missing_fields_and_preserves_searches():
    from app.modules.concierge.ports.recommendations import RecommendationOutcome
    from tests.concierge.test_intent_extraction import (
        FakeGateway,
        FakeLedger,
        extraction_json,
    )
    from tests.concierge.test_intent_extraction import service as extraction_service

    service = setup()
    gateway, ledger = FakeGateway(), FakeLedger()
    saver = InMemorySaver()

    @contextmanager
    def checkpoints():
        yield saver

    class Recommendations:
        calls = 0

        def recommend(self, *args, **kwargs):
            self.calls += 1
            return RecommendationOutcome(
                "empty", "Resultado da busca no catálogo.", None
            )

    recommendations = Recommendations()
    workflow = SessionWorkflow(
        InMemorySessionStore(),
        ContextReferenceService(service.catalog, "test", 1800),
        checkpoints,
        demand_service=service,
        intent_extraction=extraction_service(gateway, ledger),
        recommendations=recommendations,
    )
    simulator = SessionSimulator(workflow)

    def output(action="none", title=None, **extra):
        gateway.output_json = extraction_json(
            cleared_fields=[],
            rejections=[],
            rejection_ambiguous=False,
            demand_action=action,
            demand_title=title,
            demand_id=None,
            **extra,
        )

    output("none", title="Chrono Trigger", platform="SNES", mode="purchase")
    search = simulator.send("owner", "Quero conhecer Chrono Trigger para SNES, compra")
    assert "Resultado da busca" in search.reply_text
    assert not service.store.rows and recommendations.calls == 1

    output("register")
    first = simulator.send("owner", "Quero acompanhar a disponibilidade de um jogo")
    assert "Qual é o título" in first.reply_text
    assert not service.store.rows

    output("none", title="Chrono Trigger")
    assert "Qual é a plataforma" in simulator.send("owner", "Chrono Trigger").reply_text
    output("none", platform="SNES")
    assert "compra ou aluguel" in simulator.send("owner", "SNES").reply_text
    state = saver.get_tuple(
        {"configurable": {"thread_id": str(first.session_id)}}
    ).checkpoint["channel_values"]
    assert state["demand_draft"] == {"title": "Chrono Trigger", "platform": "SNES"}

    output("none")
    assert (
        "compra ou aluguel" in simulator.send("owner", "Compra ou aluguel").reply_text
    )
    assert not service.store.rows

    output("none", genre="Aventura", mode="rental")
    assert (
        "Resultado da busca"
        in simulator.send(
            "owner", "Recomende jogos de aventura para aluguel"
        ).reply_text
    )
    assert recommendations.calls == 2 and not service.store.rows

    output("none", mode="rental")
    reply = simulator.send("owner", "Aluguel")
    demand = next(iter(service.store.rows.values()))
    assert (demand.title, demand.platform, demand.mode, demand.status) == (
        "Chrono Trigger",
        "SNES",
        "rental",
        "proposed",
    )
    assert "/confirmar_demanda" in reply.reply_text
    state = saver.get_tuple(
        {"configurable": {"thread_id": str(first.session_id)}}
    ).checkpoint["channel_values"]
    assert state["demand_draft"] is None
