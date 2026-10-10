"""Versioned event matching and current Commerce facts for consented notices."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from app.modules.concierge.application.demand import DemandService
from app.modules.concierge.domain.demand import (
    DemandNotification,
    clean_description,
    normalize,
)
from app.platform.outbox.ports import EventReader, PublicEvent

CONSUMER = "concierge.demands.v1"


class DemandNotificationService:
    def __init__(
        self, demands: DemandService, events: EventReader, *, public_site_url: str = ""
    ) -> None:
        self.demands, self.events = demands, events
        self.public_site_url = public_site_url.rstrip("/")

    def process_events(self) -> None:
        for event in self.events.claim(consumer=CONSUMER):
            if not self.compatible(event):
                self.events.finish(event, consumer=CONSUMER, incompatible=True)
                continue
            try:
                game = self.demands.catalog.get_game(event.aggregate_id)
                if game is not None:
                    for demand in self.demands.store.active():
                        if (
                            demand.consented_at is None
                            or event.occurred_at < demand.consented_at
                        ):
                            continue
                        if demand.game_id is not None:
                            matches = demand.game_id == game.id
                        else:
                            matches = (
                                normalize(demand.title) == normalize(game.title)
                                and normalize(demand.platform)
                                == normalize(game.platform)
                                and self.demands.resolve(demand.title, demand.platform)
                                == game.id
                            )
                        if not matches:
                            continue
                        if (
                            event.topic == "commerce.availability.v1"
                            and event.payload.get("mode") != demand.mode
                        ):
                            continue
                        unavailable = self.demands.unavailable(game.id, demand.mode)
                        if event.payload.get("state") == "sold" and unavailable:
                            self.demands.store.close_sold(demand.id, event_id=event.id)
                        elif not unavailable:
                            self.demands.store.enqueue(
                                demand.id, event_id=event.id, game_id=game.id
                            )
                self.events.finish(event, consumer=CONSUMER)
            except Exception:
                self.events.finish(event, consumer=CONSUMER, retry=True)

    @staticmethod
    def compatible(event: PublicEvent) -> bool:
        payload = event.payload
        if type(payload) is not dict:
            return False
        try:
            if (
                type(payload.get("event_id")) is not str
                or UUID(cast(str, payload["event_id"])) != event.id
            ):
                return False
            if (
                type(payload.get("game_id")) is not str
                or UUID(cast(str, payload["game_id"])) != event.aggregate_id
            ):
                return False
            if event.topic in {"catalog.game.published.v1", "catalog.game.retired.v1"}:
                return (
                    type(payload.get("version")) is int
                    and cast(int, payload["version"]) > 0
                    and type(payload.get("etag")) is str
                    and payload.get("state")
                    == (
                        "published"
                        if event.topic == "catalog.game.published.v1"
                        else "retired"
                    )
                )
            required = {
                "version",
                "event_id",
                "game_id",
                "unit_id",
                "mode",
                "state",
                "occurred_at",
            }
            if (
                event.topic != "commerce.availability.v1"
                or set(payload) != required
                or payload["version"] != "availability.v1"
            ):
                return False
            if not all(type(payload[field]) is str for field in required):
                return False
            UUID(cast(str, payload["unit_id"]))
            occurred = datetime.fromisoformat(
                cast(str, payload["occurred_at"]).replace("Z", "+00:00")
            )
            return (
                occurred.tzinfo is not None
                and payload["state"] in {"available", "unavailable", "sold"}
                and payload["mode"] in {"purchase", "rental"}
            )
        except ValueError, TypeError, KeyError:
            return False

    def claim(
        self, *, channel: str | None = None, limit: int = 20
    ) -> list[DemandNotification]:
        return self.demands.store.claim(
            now=datetime.now(UTC), channel=channel, limit=limit
        )

    def prepare(self, notification: DemandNotification) -> str | None:
        game_id = notification.demand.game_id
        if game_id is None:
            return None
        game = self.demands.catalog.get_game(game_id)
        if game is None:
            return None
        offers = [
            offer
            for offer in self.demands.offers.list_offers([game_id]).get(game_id, [])
            if offer.mode == notification.demand.mode and offer.available_units > 0
        ]
        if not offers:
            return None
        offer = offers[0]
        price = f"{offer.price_minor / 100:.2f}".replace(".", ",")
        mode = "compra" if offer.mode == "purchase" else "aluguel"
        return f"Sandbox: {clean_description(game.title)} — {clean_description(game.platform)} disponível para {mode}, R$ {price}. Condição: {clean_description(offer.condition_summary)}. Disponibilidade pode mudar. {self.public_site_url}/games/{game.id}\nEste foi seu primeiro aviso entregue; o interesse será encerrado. Novo aviso exige novo consentimento. Sem reserva, prioridade, garantia de aquisição ou prazo."
