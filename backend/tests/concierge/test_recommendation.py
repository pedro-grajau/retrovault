from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.modules.catalog.application.discovery import (
    PublicDiscovery,
    RecommendationCriteria,
)
from app.modules.catalog.domain.publication import PublishedGame
from app.modules.commerce.domain.offers import Offer
from app.modules.concierge.application.recommendation import RecommendationService
from app.modules.concierge.domain.ai_ledger import AiPricing, ReservationGrant
from app.modules.concierge.domain.intent import Intent
from app.modules.concierge.domain.recommendation import RecommendationPlan
from app.modules.concierge.ports.models import ModelRanking, ModelUsage


def game(*, version: int = 1) -> PublishedGame:
    return PublishedGame(
        id=GAME_ID,
        title="Adventure Quest",
        platform="SNES",
        editorial={"attributes": {"genre": "Adventure"}},
        source="fixture",
        source_record_id="adventure-quest",
        version=version,
        etag="a" * 64,
        verified_at=datetime.now(UTC),
        cover_hash="b" * 64,
        cover_content_type="image/png",
        cover_attribution="fixture",
    )


GAME_ID = uuid4()
OFFER_ID = uuid4()


def offer(
    *,
    price: int = 1500,
    mode: str = "rental",
    units: int = 2,
) -> Offer:
    return Offer(
        id=OFFER_ID,
        game_id=GAME_ID,
        mode=mode,  # type: ignore[arg-type]
        price_minor=price,
        currency="BRL",
        condition_summary="Bom estado",
        available_units=units,
        demo_rank=1,
        sandbox=True,
    )


class FakeCatalog:
    def __init__(self, current: PublishedGame | None = None) -> None:
        self.current = current or game()
        self.games = [self.current]

    def list_games(self, *, limit: int, cursor=None, platform=None, genre=None):
        values = [
            item for item in self.games
            if (platform is None or item.platform.casefold() == platform.casefold())
            and (
                genre is None
                or item.editorial["attributes"]["genre"].casefold() == genre.casefold()
            )
        ]
        return values[:limit], None

    def search_games(
        self, *, query, limit, cursor=None, platform=None, genre=None, cursor_context=None
    ):
        values, _ = self.list_games(
            limit=limit, cursor=cursor, platform=platform, genre=genre
        )
        return (
            [
                type("Hit", (), {
                    "game": item,
                    "cursor_after": str(item.id),
                    "matched_fields": ("title",),
                })()
                for item in values
                if query.casefold() in item.title.casefold()
            ],
            None,
        )

    def get_game(self, game_id):
        return self.current if self.current.id == game_id else None


class FakeCommerce:
    def __init__(self, current: list[Offer] | None = None) -> None:
        self.current = current or [offer()]

    def list_offers(self, game_ids):
        return {
            game_id: [offer for offer in self.current if offer.game_id == game_id]
            for game_id in game_ids
        }


class FakeLedger:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.operations: list[str] = []
        self.reconciliations = 0

    def reserve(self, *, operation="intent_extraction", **kwargs):
        self.operations.append(operation)
        return ReservationGrant(
            uuid4(),
            "reserved" if self.allowed else "released",
            created=self.allowed,
            allowed=self.allowed,
        )

    def reconcile(self, reservation_id, *, input_tokens, output_tokens, actual_cost_usd, now):
        self.reconciliations += 1
        return self.allowed

    def release(self, reservation_id, *, now):
        return True


class FakeGateway:
    def __init__(self, output: object | None = None) -> None:
        self.output = output or {
            "recommendations": [
                {
                    "game_id": str(GAME_ID),
                    "evidence": ["intent:genre", "intent:mode", f"commerce:offer:{OFFER_ID}"],
                }
            ]
        }
        self.seen_candidates: list[dict[str, object]] = []
        self.provider_calls = 0

    def ranking_input_token_upper_bound(self, intent, candidates):
        self.seen_candidates = candidates
        return 100

    def rank_recommendations(self, intent, candidates, *, correlation_id):
        self.provider_calls += 1
        return ModelRanking(
            json.dumps(self.output),
            ModelUsage(input_tokens=50, output_tokens=20),
        )


def service(*, catalogue=None, commerce=None, gateway=None, ledger=None):
    return RecommendationService(
        gateway or FakeGateway(),
        ledger or FakeLedger(),
        PublicDiscovery(catalogue or FakeCatalog(), commerce or FakeCommerce()),
        pricing=AiPricing(
            input_usd_per_million_tokens=Decimal("1"),
            output_usd_per_million_tokens=Decimal("2"),
            max_input_tokens=1000,
            max_output_tokens=100,
        ),
        model_snapshot="gpt-4.1-mini-2025-04-14",
        configuration_version="test-config.v1",
        workflow_version="2.3.v1",
        public_site_url="https://shop.example",
    )


def test_discovery_filters_mode_price_and_availability_before_limit() -> None:
    catalog = FakeCatalog()
    commerce = FakeCommerce(
        [
            offer(price=500, mode="purchase", units=4),
            offer(price=1500, mode="rental", units=2),
            offer(price=1000, mode="rental", units=0),
        ]
    )

    candidates = PublicDiscovery(catalog, commerce).recommend_candidates(
        RecommendationCriteria(
            platform="SNES", genre="Adventure", mode="rental",
            price_min_brl_cents=1000, price_max_brl_cents=2000,
        )
    )

    assert len(candidates) == 1
    assert [(item.mode, item.price_minor, item.available_units) for item in candidates[0].offers] == [
        ("rental", 1500, 2)
    ]


def test_text_discovery_scans_past_ineligible_first_page() -> None:
    games = [PublishedGame(**{
        **game().__dict__, "id": uuid4(), "title": f"Adventure {index}",
    }) for index in range(21)]

    class PagedCatalog(FakeCatalog):
        def search_games(self, *, query, limit, cursor=None, platform=None, genre=None, cursor_context=None):
            offset = int(cursor or "0")
            page = games[offset:offset + 20]
            next_cursor = str(offset + len(page)) if offset + len(page) < len(games) else None
            return [type("Hit", (), {
                "game": item, "cursor_after": str(offset + index + 1),
                "matched_fields": ("title",),
            })() for index, item in enumerate(page)], next_cursor

    available = Offer(
        id=uuid4(), game_id=games[-1].id, mode="purchase", price_minor=1000,
        currency="BRL", condition_summary="Bom", available_units=1,
        demo_rank=1, sandbox=True,
    )
    candidates = PublicDiscovery(PagedCatalog(), FakeCommerce([available])).recommend_candidates(
        RecommendationCriteria(query="Adventure")
    )
    assert [item.game.id for item in candidates] == [games[-1].id]


def test_text_and_structured_searches_share_the_500_record_scan_budget() -> None:
    games = [PublishedGame(**{
        **game().__dict__, "id": uuid4(), "title": f"Adventure {index}",
    }) for index in range(500)]

    class ScanCatalog(FakeCatalog):
        text_scanned = 0
        structured_scanned = 0

        def search_games(self, *, query, limit, cursor=None, platform=None, genre=None, cursor_context=None):
            offset = int(cursor or "0")
            page = games[offset:min(offset + limit, 300)]
            self.text_scanned += len(page)
            next_cursor = str(offset + len(page)) if offset + len(page) < 300 else None
            return [type("Hit", (), {
                "game": item,
                "cursor_after": str(offset + index + 1),
                "matched_fields": ("title",),
            })() for index, item in enumerate(page)], next_cursor

        def list_games(self, *, limit, cursor=None, platform=None, genre=None):
            offset = int(cursor or "0")
            page = games[offset:offset + limit]
            self.structured_scanned += len(page)
            next_cursor = str(offset + len(page)) if offset + len(page) < len(games) else None
            return page, next_cursor

    class EmptyCommerce:
        def list_offers(self, game_ids):
            return {}

    catalog = ScanCatalog()
    candidates = PublicDiscovery(catalog, EmptyCommerce()).recommend_candidates(
        RecommendationCriteria(query="Adventure", platform="SNES")
    )

    assert candidates == []
    assert catalog.text_scanned == 300
    assert catalog.structured_scanned == 200
    assert catalog.text_scanned + catalog.structured_scanned == 500


def test_structured_discovery_uses_offer_facts_before_twenty_candidate_cap() -> None:
    catalog = FakeCatalog()
    catalog.games = [PublishedGame(**{
        **game().__dict__, "id": uuid4(), "title": f"Adventure {index}",
    }) for index in range(25)]
    offers = [Offer(
        id=uuid4(), game_id=item.id, mode="purchase", price_minor=1000 + index,
        currency="BRL", condition_summary="Bom", available_units=index + 1,
        demo_rank=25 - index, sandbox=True,
    ) for index, item in enumerate(catalog.games)]
    candidates = PublicDiscovery(catalog, FakeCommerce(offers)).recommend_candidates(
        RecommendationCriteria()
    )
    assert len(candidates) == 20
    assert candidates[0].game.id == catalog.games[-1].id


def test_valid_ranking_composes_only_verified_facts_and_revalidates_before_delivery() -> None:
    commerce = FakeCommerce([offer(price=1500)])
    ledger = FakeLedger()
    gateway = FakeGateway()
    recommender = service(commerce=commerce, gateway=gateway, ledger=ledger)
    outcome = recommender.recommend(
        Intent(genre="Adventure", mode="rental", price_max_brl_cents=2000),
        session_id=uuid4(),
        channel="simulator",
        update_id=4,
        correlation_id=uuid4(),
    )

    assert outcome.status == "accepted"
    assert "https://shop.example/games/" + str(GAME_ID) in outcome.reply_text
    assert "R$ 15,00" in outcome.reply_text
    assert "o catálogo o classifica como Adventure" in outcome.reply_text
    assert "condition" not in outcome.reply_text
    assert ledger.operations == ["recommendation_ranking"]
    assert gateway.seen_candidates[0]["game_id"] == str(GAME_ID)

    assert outcome.context is not None
    outcome.context["greeting_required"] = True
    commerce.current = [offer(price=2500)]
    refreshed = recommender.revalidate_and_compose(outcome.context)

    assert refreshed.startswith("Oi! Sou Pixel, assistente de IA")
    assert "não consegui confirmar opções comerciais" in refreshed.casefold()
    assert outcome.context["revalidation"]["facts"][0]["status"] == "no_eligible_offer"


def test_style_evidence_does_not_claim_the_title_matched_when_editorial_text_did() -> None:
    published = PublishedGame(**{
        **game().__dict__,
        "editorial": {
            "attributes": {
                "genre": "Adventure",
                "description": "Adventure with exploration and puzzles",
            }
        },
    })

    class DescriptionCatalog(FakeCatalog):
        def __init__(self) -> None:
            super().__init__(published)

        def search_games(self, **kwargs):  # type: ignore[no-untyped-def]
            hits, cursor = super().search_games(**kwargs)
            return [
                type("Hit", (), {
                    "game": hit.game,
                    "cursor_after": hit.cursor_after,
                    "matched_fields": ("description",),
                })()
                for hit in hits
            ], cursor

    gateway = FakeGateway({"recommendations": [{
        "game_id": str(GAME_ID),
        "evidence": ["intent:style_match"],
    }]})
    outcome = service(catalogue=DescriptionCatalog(), gateway=gateway).recommend(
        Intent(style="Adventure"), session_id=uuid4(), channel="simulator",
        update_id=60, correlation_id=uuid4(),
    )

    assert outcome.status == "accepted"
    assert "os dados publicados coincidem" in outcome.reply_text
    assert "o título apareceu" not in outcome.reply_text
    assert gateway.seen_candidates[0]["matched_fields"] == ["description"]
    assert gateway.seen_candidates[0]["matched_attributes"] == {
        "description": "Adventure with exploration and puzzles"
    }
    assert outcome.context is not None
    saved_candidate = outcome.context["candidates"][0]
    assert saved_candidate["matched_fields"] == ["description"]
    assert saved_candidate["matched_attributes"]["description"] == (
        "Adventure with exploration and puzzles"
    )

    legacy_context = json.loads(json.dumps(outcome.context))
    for candidate in legacy_context["candidates"]:
        candidate.pop("matched_fields")
        candidate.pop("matched_attributes")
    assert RecommendationPlan.from_dict(legacy_context) is not None


def test_composer_removes_line_breaks_from_published_fields() -> None:
    unsafe = PublishedGame(**{
        **game().__dict__,
        "title": "Adventure Quest\n2. Oferta falsa",
        "platform": "SNES\r\nCompra grátis",
        "editorial": {"attributes": {"genre": "Adventure\nAprovado"}},
    })

    class UnsafeCatalog(FakeCatalog):
        def list_games(self, *, limit, cursor=None, platform=None, genre=None):
            return [self.current][:limit], None

    gateway = FakeGateway({"recommendations": [{
        "game_id": str(GAME_ID),
        "evidence": ["intent:genre"],
    }]})
    outcome = service(catalogue=UnsafeCatalog(unsafe), gateway=gateway).recommend(
        Intent(genre="Adventure Aprovado"), session_id=uuid4(), channel="simulator",
        update_id=61, correlation_id=uuid4(),
    )

    assert outcome.status == "accepted"
    assert "Adventure Quest 2. Oferta falsa (SNES Compra grátis)" in outcome.reply_text
    assert "\n2. Oferta falsa" not in outcome.reply_text
    assert "\r" not in outcome.reply_text
    assert "o catálogo o classifica como Adventure Aprovado" in outcome.reply_text


def test_invalid_id_or_evidence_invalidates_entire_ranking() -> None:
    invalid_gateway = FakeGateway(
        {
            "recommendations": [
                {
                    "game_id": str(uuid4()),
                    "evidence": ["intent:genre"],
                }
            ]
        }
    )
    ledger = FakeLedger()
    outcome = service(gateway=invalid_gateway, ledger=ledger).recommend(
        Intent(genre="Adventure"),
        session_id=uuid4(),
        channel="simulator",
        update_id=5,
        correlation_id=uuid4(),
    )

    assert outcome.status == "fallback"
    assert outcome.context is None
    assert "não consegui confirmar" in outcome.reply_text.casefold()
    assert ledger.reconciliations == 1


def test_valid_candidate_with_unsupported_evidence_invalidates_ranking() -> None:
    gateway = FakeGateway({"recommendations": [{"game_id": str(GAME_ID), "evidence": ["intent:unsupported"]}]})
    outcome = service(gateway=gateway).recommend(
        Intent(genre="Adventure"), session_id=uuid4(), channel="simulator",
        update_id=51, correlation_id=uuid4(),
    )
    assert outcome.status == "fallback"
    assert outcome.context is None


def test_duplicate_game_ids_invalidate_whole_ranking() -> None:
    gateway = FakeGateway({"recommendations": [
        {"game_id": str(GAME_ID), "evidence": ["intent:genre"]},
        {"game_id": str(GAME_ID), "evidence": ["intent:genre"]},
    ]})
    outcome = service(gateway=gateway).recommend(
        Intent(genre="Adventure"), session_id=uuid4(), channel="simulator",
        update_id=52, correlation_id=uuid4(),
    )
    assert outcome.status == "fallback"


def test_denied_ranking_reservation_does_not_call_provider() -> None:
    gateway = FakeGateway()
    outcome = service(gateway=gateway, ledger=FakeLedger(allowed=False)).recommend(
        Intent(genre="Adventure"), session_id=uuid4(), channel="simulator",
        update_id=53, correlation_id=uuid4(),
    )
    assert outcome.status == "fallback"
    assert gateway.provider_calls == 0


def test_empty_ranking_is_a_valid_no_match() -> None:
    gateway = FakeGateway({"recommendations": []})
    outcome = service(gateway=gateway).recommend(
        Intent(genre="Adventure"), session_id=uuid4(), channel="simulator",
        update_id=54, correlation_id=uuid4(),
    )
    assert outcome.status == "empty"
    assert "recomendação adequada" in outcome.reply_text
    assert "opções comerciais elegíveis" in outcome.reply_text
    assert outcome.context is None


def test_unverifiable_constraints_skip_ranking() -> None:
    gateway = FakeGateway()
    outcome = service(gateway=gateway).recommend(
        Intent(genre="Adventure", constraints=("single player",)),
        session_id=uuid4(), channel="simulator", update_id=55,
        correlation_id=uuid4(),
    )
    assert outcome.status == "empty"
    assert "não permite confirmar" in outcome.reply_text
    assert gateway.provider_calls == 0


def test_style_query_miss_merges_matching_structured_candidates() -> None:
    catalog = FakeCatalog()
    gateway = FakeGateway({"recommendations": [{"game_id": str(GAME_ID), "evidence": ["intent:genre"]}]})
    outcome = service(catalogue=catalog, gateway=gateway).recommend(
        Intent(style="metroidvania atmosferico", platform="SNES", genre="Adventure"),
        session_id=uuid4(), channel="simulator", update_id=56,
        correlation_id=uuid4(),
    )
    assert outcome.status == "accepted"
    assert gateway.seen_candidates[0]["game_id"] == str(GAME_ID)


def test_weak_text_hit_merges_structured_candidates_with_candidate_specific_evidence() -> None:
    catalog = FakeCatalog()
    structured_only = PublishedGame(**{
        **game().__dict__, "id": uuid4(), "title": "Quest of the Moon",
    })
    catalog.games = [catalog.current, structured_only]
    commerce = FakeCommerce([
        offer(),
        Offer(
            id=uuid4(), game_id=structured_only.id, mode="purchase", price_minor=1800,
            currency="BRL", condition_summary="Bom", available_units=1,
            demo_rank=2, sandbox=True,
        ),
    ])
    gateway = FakeGateway({"recommendations": [{"game_id": str(GAME_ID), "evidence": ["intent:style_match"]}]})
    outcome = service(catalogue=catalog, commerce=commerce, gateway=gateway).recommend(
        Intent(style="Adventure", genre="Adventure"),
        session_id=uuid4(), channel="simulator", update_id=59,
        correlation_id=uuid4(),
    )
    assert outcome.status == "accepted"
    assert len(gateway.seen_candidates) == 2
    assert "intent:style_match" in gateway.seen_candidates[0]["evidence_refs"]
    assert "intent:style_match" not in gateway.seen_candidates[1]["evidence_refs"]


def test_long_condition_is_accepted_and_reply_remains_bounded() -> None:
    long_offer = Offer(
        **{**offer().__dict__, "condition_summary": "condição " * 500}
    )
    commerce = FakeCommerce([long_offer])
    gateway = FakeGateway({"recommendations": [{
        "game_id": str(GAME_ID),
        "evidence": ["intent:genre", f"commerce:offer:{OFFER_ID}"],
    }]})
    outcome = service(commerce=commerce, gateway=gateway).recommend(
        Intent(genre="Adventure"), session_id=uuid4(), channel="simulator",
        update_id=57, correlation_id=uuid4(),
    )
    assert outcome.status == "accepted"
    assert len(outcome.reply_text) <= 4096
    assert "..." in outcome.reply_text


def test_long_composed_reply_fails_safely_before_persistence() -> None:
    catalog = FakeCatalog(PublishedGame(**{
        **game().__dict__, "title": "J" * 200,
    }))
    recommender = service(catalogue=catalog)
    recommender.public_site_url = "https://shop.example/" + "x" * 4200
    outcome = recommender.recommend(
        Intent(genre="Adventure"), session_id=uuid4(), channel="simulator",
        update_id=58, correlation_id=uuid4(),
    )
    assert outcome.status == "fallback"
    assert outcome.context is None


def test_model_never_receives_more_than_twenty_candidates() -> None:
    catalog = FakeCatalog()
    catalog.games = [
        PublishedGame(
            **{
                **game().__dict__,
                "id": uuid4(),
                "title": f"Adventure Quest {index}",
            }
        )
        for index in range(25)
    ]
    commerce = FakeCommerce()
    commerce.current = [
        Offer(
            id=uuid4(),
            game_id=item.id,
            mode="purchase",
            price_minor=1500,
            currency="BRL",
            condition_summary="Bom estado",
            available_units=1,
            demo_rank=1,
            sandbox=True,
        )
        for item in catalog.games
    ]
    gateway = FakeGateway(
        {
            "recommendations": [
                {
                    "game_id": str(catalog.games[0].id),
                    "evidence": ["intent:genre"],
                }
            ]
        }
    )

    outcome = service(catalogue=catalog, commerce=commerce, gateway=gateway).recommend(
        Intent(genre="Adventure"),
        session_id=uuid4(),
        channel="simulator",
        update_id=6,
        correlation_id=uuid4(),
    )

    assert outcome.status == "accepted"
    assert len(gateway.seen_candidates) <= 20
