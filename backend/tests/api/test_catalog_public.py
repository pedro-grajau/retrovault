from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import httpx
import pytest

from app.main import app
from app.modules.catalog.adapters.postgres_repository import PostgresCatalogRepository
from app.modules.catalog.api import router as catalog_router
from app.modules.catalog.application.discovery import PublicDiscovery
from app.modules.catalog.domain.publication import PublishedGame
from app.modules.commerce.domain.offers import Offer
from app.modules.commerce.ports.offers import CommerceReadUnavailable


class FakePublishedCatalog:
    def __init__(self, games: PublishedGame | list[PublishedGame] | None) -> None:
        if games is None:
            self.games = []
        elif isinstance(games, list):
            self.games = sorted(games, key=lambda game: game.id)
        else:
            self.games = [games]
        self.game = self.games[0] if len(self.games) == 1 else None

    @staticmethod
    def encode_cursor(game: PublishedGame) -> str:
        return PostgresCatalogRepository.encode_cursor(game)

    def list_games(self, *, limit: int, cursor=None, platform=None, genre=None):
        cursor_id = PostgresCatalogRepository._cursor_decode(cursor)
        games = [
            game
            for game in self.games
            if (cursor_id is None or game.id > cursor_id)
            and (platform is None or game.platform.lower() == platform.lower())
            and (
                genre is None
                or game.editorial["attributes"].get("genre", "").lower()
                == genre.lower()
            )
        ]
        page = games[:limit]
        next_cursor = (
            PostgresCatalogRepository.encode_cursor(page[-1])
            if page and len(games) > limit
            else None
        )
        return page, next_cursor

    def get_game(self, game_id):
        return next((game for game in self.games if game.id == game_id), None)

    def get_cover(self, game_id):
        game = self.get_game(game_id)
        if game is None:
            return None
        return b"published-cover", "image/png", game.cover_hash


class FakeCommerceReader:
    def __init__(
        self,
        offers_by_game: dict[UUID, list[Offer]] | None = None,
        *,
        unavailable: bool = False,
    ) -> None:
        self.offers_by_game = offers_by_game or {}
        self.unavailable = unavailable

    def list_offers(self, game_ids):
        if self.unavailable:
            raise CommerceReadUnavailable
        return {
            game_id: self.offers_by_game[game_id]
            for game_id in game_ids
            if game_id in self.offers_by_game
        }


def _wire_public_catalog(monkeypatch, catalog, commerce=None):
    commerce = commerce or FakeCommerceReader()
    monkeypatch.setattr(catalog_router, "_catalog", catalog)
    monkeypatch.setattr(
        catalog_router, "_discovery", PublicDiscovery(catalog, commerce)
    )


def _game(
    game_id: UUID | None = None,
    *,
    title: str = "Jogo publicado",
    platform: str = "SNES",
    genre: str = "Aventura",
) -> PublishedGame:
    return PublishedGame(
        id=game_id or uuid4(),
        title=title,
        platform=platform,
        editorial={
            "attributes": {
                "title": title,
                "platform": platform,
                "genre": genre,
            },
            "lineage": {
                "title": {
                    "source": "human-review",
                    "source_version": "3",
                    "source_record_id": "42",
                    "captured_at": "2026-09-30T12:00:00+00:00",
                    "previous_value": "Título privado anterior",
                    "reason": "Motivo interno da correção",
                    "previous_etag": "review-etag-old",
                    "resulting_etag": "review-etag-new",
                    "actor": "Eduardo",
                    "corrected_at": "2026-09-30T12:01:00+00:00",
                }
            },
        },
        source="retroachievements",
        source_record_id="42",
        version=1,
        etag="a" * 64,
        verified_at=datetime(2026, 9, 30, tzinfo=UTC),
        cover_hash="b" * 64,
        cover_content_type="image/png",
        cover_attribution="RetroAchievements",
    )


def _offer(
    game_id: UUID,
    mode: Literal["purchase", "rental"],
    *,
    rank: int,
    available_units: int = 1,
) -> Offer:
    return Offer(
        id=uuid4(),
        game_id=game_id,
        mode=mode,
        price_minor=4990 if mode == "purchase" else 990,
        currency="BRL",
        condition_summary="Condição demonstrativa Sandbox.",
        available_units=available_units,
        demo_rank=rank,
        sandbox=True,
    )


@pytest.mark.anyio
async def test_public_catalog_exposes_only_published_projection(monkeypatch) -> None:
    game = _game()
    _wire_public_catalog(monkeypatch, FakePublishedCatalog(game))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/catalog/games")

    assert response.status_code == 200
    payload = response.json()["items"][0]
    assert payload["id"] == str(game.id)
    origin = payload["origin_by_attribute"]["title"]
    assert origin["source"] == "human-review"
    assert origin["source_version"] == "3"
    assert origin["source_record_id"] == "42"
    assert origin["captured_at"] == "2026-09-30T12:00:00+00:00"
    assert "previous_value" not in origin
    assert "reason" not in origin
    assert "previous_etag" not in origin
    assert "resulting_etag" not in origin
    assert "corrected_at" not in origin
    assert payload["cover_attribution"] == "RetroAchievements"
    assert "raw_payload" not in payload
    assert "staging" not in payload
    assert "stock" not in payload


@pytest.mark.anyio
async def test_public_catalog_returns_empty_collection_without_published_games(
    monkeypatch,
) -> None:
    _wire_public_catalog(monkeypatch, FakePublishedCatalog(None))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/catalog/games")

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json().get("next_cursor") is None


@pytest.mark.anyio
async def test_public_catalog_serializes_populated_sandbox_offers(monkeypatch) -> None:
    game = _game(UUID(int=500))
    offer = _offer(game.id, "purchase", rank=7, available_units=2)
    _wire_public_catalog(
        monkeypatch,
        FakePublishedCatalog(game),
        FakeCommerceReader({game.id: [offer]}),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/catalog/games")

    assert response.status_code == 200
    payload = response.json()
    assert payload["commerce_status"] == "available"
    assert payload["items"][0]["offers"] == [
        {
            "id": str(offer.id),
            "mode": "purchase",
            "price_minor": 4990,
            "currency": "BRL",
            "condition_summary": "Condição demonstrativa Sandbox.",
            "available_units": 2,
            "demo_rank": 7,
            "sandbox": True,
        }
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("availability", "expected_ids", "expected_units"),
    [
        ("available", [str(UUID(int=102)), str(UUID(int=104))], [1, 2]),
        ("unavailable", [str(UUID(int=101)), str(UUID(int=103))], [0, 0]),
    ],
)
async def test_public_catalog_filters_availability_and_continues_after_last_match(
    monkeypatch,
    availability: str,
    expected_ids: list[str],
    expected_units: list[int],
) -> None:
    games = [_game(UUID(int=game_id), title=f"Jogo {game_id}") for game_id in range(101, 105)]
    offers = {
        games[0].id: [_offer(games[0].id, "purchase", rank=1, available_units=0)],
        games[1].id: [_offer(games[1].id, "purchase", rank=2, available_units=1)],
        games[2].id: [_offer(games[2].id, "rental", rank=3, available_units=0)],
        games[3].id: [_offer(games[3].id, "rental", rank=4, available_units=2)],
    }
    _wire_public_catalog(
        monkeypatch,
        FakePublishedCatalog(games),
        FakeCommerceReader(offers),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            "/api/v1/catalog/games",
            params={"limit": 1, "availability": availability},
        )
        first_payload = first.json()
        second = await client.get(
            "/api/v1/catalog/games",
            params={
                "limit": 1,
                "availability": availability,
                "cursor": first_payload["next_cursor"],
            },
        )
        second_payload = second.json()

    assert first.status_code == second.status_code == 200
    assert first_payload["next_cursor"]
    assert second_payload.get("next_cursor") is None
    items = first_payload["items"] + second_payload["items"]
    assert [item["id"] for item in items] == expected_ids
    assert [item["offers"][0]["available_units"] for item in items] == expected_units


@pytest.mark.anyio
async def test_demo_popular_uses_rank_then_mode_variety_and_paginates(monkeypatch) -> None:
    games = [_game(UUID(int=game_id), title=f"Jogo {game_id}") for game_id in range(200, 204)]
    offers = {
        games[0].id: [_offer(games[0].id, "purchase", rank=0)],
        games[1].id: [
            _offer(games[1].id, "purchase", rank=1),
            _offer(games[1].id, "rental", rank=1),
        ],
        games[2].id: [_offer(games[2].id, "purchase", rank=1)],
        games[3].id: [
            _offer(games[3].id, "purchase", rank=5),
            _offer(games[3].id, "rental", rank=5),
        ],
    }
    _wire_public_catalog(
        monkeypatch,
        FakePublishedCatalog(games),
        FakeCommerceReader(offers),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            "/api/v1/catalog/games",
            params={"limit": 2, "sort": "demo_popular", "availability": "available"},
        )
        first_payload = first.json()
        second = await client.get(
            "/api/v1/catalog/games",
            params={
                "limit": 2,
                "sort": "demo_popular",
                "availability": "available",
                "cursor": first_payload["next_cursor"],
            },
        )
        second_payload = second.json()

    assert first.status_code == second.status_code == 200
    assert [item["id"] for item in first_payload["items"]] == [
        str(UUID(int=200)),
        str(UUID(int=201)),
    ]
    assert first_payload["next_cursor"]
    assert [item["id"] for item in second_payload["items"]] == [
        str(UUID(int=202)),
        str(UUID(int=203)),
    ]
    assert second_payload.get("next_cursor") is None


@pytest.mark.anyio
async def test_commerce_failure_hides_offers_or_rejects_availability_filter(monkeypatch) -> None:
    game = _game(UUID(int=600))
    _wire_public_catalog(
        monkeypatch,
        FakePublishedCatalog(game),
        FakeCommerceReader(unavailable=True),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unfiltered = await client.get("/api/v1/catalog/games")
        filtered = await client.get(
            "/api/v1/catalog/games", params={"availability": "available"}
        )

    assert unfiltered.status_code == 200
    unfiltered_item = unfiltered.json()["items"][0]
    assert unfiltered.json()["commerce_status"] == "unavailable"
    assert "offers" not in unfiltered_item
    assert filtered.status_code == 503


@pytest.mark.anyio
async def test_public_game_etag_and_unpublished_not_found(monkeypatch) -> None:
    game = _game()
    monkeypatch.setattr(catalog_router, "_catalog", FakePublishedCatalog(game))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/v1/catalog/games/{game.id}")
        cached = await client.get(
            f"/api/v1/catalog/games/{game.id}",
            headers={"If-None-Match": f'W/"{game.etag}"'},
        )
        missing = await client.get(f"/api/v1/catalog/games/{uuid4()}")

    assert response.status_code == 200
    assert response.headers["ETag"] == f'"{game.etag}"'
    assert response.headers["Cache-Control"] == "public, max-age=60"
    assert cached.status_code == 304
    assert missing.status_code == 404


@pytest.mark.anyio
async def test_cover_route_serves_only_published_media(monkeypatch) -> None:
    game = _game()
    monkeypatch.setattr(catalog_router, "_catalog", FakePublishedCatalog(game))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/v1/catalog/games/{game.id}/box-art")
        cached = await client.get(
            f"/api/v1/catalog/games/{game.id}/box-art",
            headers={"If-None-Match": f'W/"{game.cover_hash}"'},
        )
        missing = await client.get(f"/api/v1/catalog/games/{uuid4()}/box-art")

    assert response.status_code == 200
    assert response.content == b"published-cover"
    assert response.headers["Content-Type"] == "image/png"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "public, max-age=60"
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["ETag"] == f'"{game.cover_hash}"'
    assert missing.status_code == 404


@pytest.mark.anyio
async def test_public_list_rejects_invalid_cursor(monkeypatch) -> None:
    _wire_public_catalog(monkeypatch, FakePublishedCatalog(_game()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/catalog/games?cursor=not-a-cursor")

    assert response.status_code == 400
    assert response.json()["code"] == "http_error"


def test_catalog_openapi_documents_public_error_and_not_modified_responses() -> None:
    schema = app.openapi()
    paths = schema["paths"]

    detail = paths["/api/v1/catalog/games/{game_id}"]["get"]["responses"]
    box_art = paths["/api/v1/catalog/games/{game_id}/box-art"]["get"]["responses"]
    game_list = paths["/api/v1/catalog/games"]["get"]["responses"]
    schemas = schema["components"]["schemas"]

    assert "ETag" in detail["200"]["headers"]
    assert "ETag" in detail["304"]["headers"]
    assert "304" in detail
    assert "404" in detail
    assert "503" in game_list
    assert "304" in box_art
    assert "404" in box_art
    assert "ETag" in box_art["200"]["headers"]
    assert "ETag" in box_art["304"]["headers"]
    assert set(box_art["200"]["content"]) == {
        "image/png", "image/jpeg", "image/webp"
    }
    assert all(
        media["schema"] == {"type": "string", "format": "binary"}
        for media in box_art["200"]["content"].values()
    )
    attributes = schemas["GameAttributes"]
    assert attributes["additionalProperties"] is False
    assert attributes["properties"]["included_items"]["anyOf"]
    origin = schemas["GameResponse"]["properties"]["origin_by_attribute"]
    assert origin["additionalProperties"]["$ref"].endswith("/AttributeOrigin")


@pytest.mark.anyio
async def test_catalog_cors_allows_conditional_requests_and_exposes_etag(monkeypatch) -> None:
    game = _game()
    monkeypatch.setattr(catalog_router, "_catalog", FakePublishedCatalog(game))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        preflight = await client.options(
            f"/api/v1/catalog/games/{game.id}",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "If-None-Match",
            },
        )
        response = await client.get(
            f"/api/v1/catalog/games/{game.id}",
            headers={"Origin": "http://localhost:5173"},
        )

    assert preflight.status_code == 200
    assert "if-none-match" in preflight.headers["access-control-allow-headers"].lower()
    assert response.status_code == 200
    assert response.headers["ETag"] == f'"{game.etag}"'
    assert "etag" in response.headers["access-control-expose-headers"].lower()
