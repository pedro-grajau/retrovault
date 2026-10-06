import base64
import json
import re
import unicodedata
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Literal
from uuid import UUID, uuid4

import httpx
import pytest

from app.main import app
from app.modules.catalog.adapters.postgres_repository import PostgresCatalogRepository
from app.modules.catalog.api import router as catalog_router
from app.modules.catalog.application.discovery import PublicDiscovery
from app.modules.catalog.domain.publication import PublishedGame
from app.modules.catalog.ports.repository import PublishedSearchHit
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
        self.search_calls = 0

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

    @staticmethod
    def _normalized(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", value.casefold())
        unaccented = "".join(
            character
            for character in decomposed
            if not unicodedata.combining(character)
        )
        return " ".join(re.findall(r"[a-z0-9]+", unaccented))

    def search_games(
        self,
        *,
        query: str,
        limit: int,
        cursor=None,
        platform=None,
        genre=None,
        cursor_context=None,
    ):
        self.search_calls += 1
        normalized_query = self._normalized(query)
        if not normalized_query:
            return [], None
        search_context = PostgresCatalogRepository._search_context_hash(
            query=query,
            platform=platform,
            genre=genre,
            cursor_context=cursor_context,
        )
        decoded = PostgresCatalogRepository._search_cursor_decode(cursor)
        if decoded is not None and decoded["context"] != search_context:
            raise ValueError("invalid_cursor")
        cursor_key = (
            (
                decoded["rank"],
                -decoded["similarity"],
                str(decoded["id"]),
            )
            if decoded is not None
            else None
        )
        matches = []
        for game in self.games:
            if platform and game.platform.strip().lower() != platform.strip().lower():
                continue
            attributes = game.editorial.get("attributes", {})
            if genre and str(attributes.get("genre", "")).strip().lower() != genre.strip().lower():
                continue
            normalized_title = self._normalized(game.title)
            similarity = SequenceMatcher(None, normalized_title, normalized_query).ratio()
            if normalized_title == normalized_query:
                rank = 0
            elif (
                normalized_query in normalized_title
                or set(normalized_query.split()).issubset(set(normalized_title.split()))
            ):
                rank = 1
            elif len(normalized_query) >= 4 and similarity >= 0.45:
                rank = 2
            else:
                continue
            key = (rank, -similarity, str(game.id))
            if cursor_key is None or key > cursor_key:
                matches.append((key, game, similarity, rank))
        matches.sort(key=lambda item: item[0])
        page = matches[:limit]
        hits = [
            PublishedSearchHit(
                game=game,
                cursor_after=PostgresCatalogRepository._search_cursor_encode(
                    rank=rank,
                    similarity=similarity,
                    game_id=game.id,
                    search_context=search_context,
                ),
            )
            for _, game, similarity, rank in page
        ]
        next_cursor = hits[-1].cursor_after if len(matches) > limit and hits else None
        return hits, next_cursor

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
    region: str | None = None,
    edition: str | None = None,
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
                "region": region,
                "edition": edition,
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


@pytest.mark.anyio
async def test_title_search_normalizes_variations_filters_platform_and_paginates(
    monkeypatch,
) -> None:
    games = [
        _game(UUID(int=701), title="Pokémon Stadium", platform="N64"),
        _game(
            UUID(int=702),
            title="Pokemon Stadium",
            platform="SNES",
            region="NTSC-J",
            edition="Player's Choice",
        ),
        _game(UUID(int=703), title="Pokémon Stadium 2", platform="SNES"),
    ]
    catalog = FakePublishedCatalog(games)
    _wire_public_catalog(monkeypatch, catalog)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            "/api/v1/catalog/games",
            params={"q": "POKEMON-STADIUM", "platform": "SNES", "limit": 1},
        )
        first_payload = first.json()
        second = await client.get(
            "/api/v1/catalog/games",
            params={
                "q": "POKEMON-STADIUM",
                "platform": " SNES ",
                "limit": 1,
                "cursor": first_payload["next_cursor"],
            },
        )

    assert first.status_code == second.status_code == 200
    assert [item["id"] for item in first_payload["items"]] == [str(UUID(int=702))]
    assert first_payload["items"][0]["platform"] == "SNES"
    assert first_payload["items"][0]["attributes"]["region"] == "NTSC-J"
    assert first_payload["items"][0]["attributes"]["edition"] == "Player's Choice"
    assert first_payload["next_cursor"]
    assert [item["id"] for item in second.json()["items"]] == [str(UUID(int=703))]
    assert second.json().get("next_cursor") is None
    assert catalog.search_calls == 2


@pytest.mark.anyio
async def test_title_search_accepts_a_small_spelling_error(monkeypatch) -> None:
    catalog = FakePublishedCatalog(
        _game(UUID(int=705), title="Pokémon Stadium")
    )
    _wire_public_catalog(monkeypatch, catalog)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/catalog/games", params={"q": "Pokemon Stadum"}
        )

    assert response.status_code == 200
    assert [game["title"] for game in response.json()["items"]] == [
        "Pokémon Stadium"
    ]


@pytest.mark.anyio
async def test_title_search_rejects_cursor_reused_with_different_query(
    monkeypatch,
) -> None:
    games = [
        _game(UUID(int=706), title="Sonic the Hedgehog"),
        _game(UUID(int=707), title="Sonic the Hedgehog 2"),
    ]
    catalog = FakePublishedCatalog(games)
    _wire_public_catalog(monkeypatch, catalog)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            "/api/v1/catalog/games", params={"q": "Sonic", "limit": 1}
        )
        mismatched = await client.get(
            "/api/v1/catalog/games",
            params={
                "q": "Hedgehog",
                "limit": 1,
                "cursor": first.json()["next_cursor"],
            },
        )

    assert first.status_code == 200
    assert first.json()["next_cursor"]
    assert mismatched.status_code == 400
    assert mismatched.json()["code"] == "http_error"


@pytest.mark.anyio
async def test_title_search_orders_exact_text_fuzzy_and_uuid_ties(monkeypatch) -> None:
    games = [
        _game(UUID(int=710), title="Sonic"),
        _game(UUID(int=709), title="Sonic"),
        _game(UUID(int=711), title="Sonic 2"),
        _game(UUID(int=712), title="Sonix"),
    ]
    _wire_public_catalog(monkeypatch, FakePublishedCatalog(games))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/catalog/games", params={"q": "Sonic"})

    assert response.status_code == 200
    assert [game["id"] for game in response.json()["items"]] == [
        str(UUID(int=709)),
        str(UUID(int=710)),
        str(UUID(int=711)),
        str(UUID(int=712)),
    ]


@pytest.mark.anyio
async def test_title_search_cursor_stays_bounded_for_long_published_titles(
    monkeypatch,
) -> None:
    catalog = FakePublishedCatalog(
        [
            _game(UUID(int=801), title=f"Sonic {'a' * 1500}"),
            _game(UUID(int=802), title=f"Sonic {'b' * 1500}"),
        ]
    )
    _wire_public_catalog(monkeypatch, catalog)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            "/api/v1/catalog/games", params={"q": "Sonic", "limit": 1}
        )
        first_payload = first.json()
        second = await client.get(
            "/api/v1/catalog/games",
            params={
                "q": "Sonic",
                "limit": 1,
                "cursor": first_payload["next_cursor"],
            },
        )

    assert first.status_code == second.status_code == 200
    assert len(first_payload["next_cursor"]) < 1024
    assert first_payload["items"][0]["id"] == str(UUID(int=801))
    assert second.json()["items"][0]["id"] == str(UUID(int=802))


@pytest.mark.anyio
async def test_title_search_rejects_overflowing_cursor_similarity_as_bad_request(
    monkeypatch,
) -> None:
    catalog = FakePublishedCatalog(_game(UUID(int=803), title="Sonic"))
    _wire_public_catalog(monkeypatch, catalog)
    raw_cursor = json.dumps(
        {
            "v": 1,
            "rank": 0,
            "similarity": 10**400,
            "id": str(UUID(int=803)),
            "context": "0" * 64,
        },
        separators=(",", ":"),
    ).encode()
    cursor = base64.urlsafe_b64encode(raw_cursor).decode().rstrip("=")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/catalog/games", params={"q": "Sonic", "cursor": cursor}
        )

    assert len(cursor) < 1024
    assert response.status_code == 400
    assert response.json()["code"] == "http_error"


@pytest.mark.anyio
@pytest.mark.parametrize("availability", ["available", "unavailable"])
async def test_title_search_filters_availability_across_scan_pages_and_cursors(
    monkeypatch,
    availability: str,
) -> None:
    games = [_game(UUID(int=900 + index), title="Sonic") for index in range(1, 104)]
    target_units = 1 if availability == "available" else 0
    offers = {
        game.id: [
            _offer(
                game.id,
                "purchase",
                rank=index,
                available_units=target_units if index > 100 else 1 - target_units,
            )
        ]
        for index, game in enumerate(games, start=1)
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
            params={"q": "Sonic", "availability": availability, "limit": 1},
        )
        second = await client.get(
            "/api/v1/catalog/games",
            params={
                "q": "Sonic",
                "availability": availability,
                "limit": 1,
                "cursor": first.json()["next_cursor"],
            },
        )
        third = await client.get(
            "/api/v1/catalog/games",
            params={
                "q": "Sonic",
                "availability": availability,
                "limit": 1,
                "cursor": second.json()["next_cursor"],
            },
        )

    assert first.status_code == second.status_code == third.status_code == 200
    assert [first.json()["items"][0]["id"], second.json()["items"][0]["id"], third.json()["items"][0]["id"]] == [
        str(UUID(int=1001)),
        str(UUID(int=1002)),
        str(UUID(int=1003)),
    ]
    assert first.json()["next_cursor"]
    assert second.json()["next_cursor"]
    assert third.json().get("next_cursor") is None


@pytest.mark.anyio
async def test_title_search_availability_reports_commerce_unavailable(monkeypatch) -> None:
    _wire_public_catalog(
        monkeypatch,
        FakePublishedCatalog(_game(UUID(int=1101), title="Sonic")),
        FakeCommerceReader(unavailable=True),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/catalog/games",
            params={"q": "Sonic", "availability": "available"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "http_error"


@pytest.mark.anyio
async def test_title_search_rejects_empty_short_and_overlong_terms_without_search(
    monkeypatch,
) -> None:
    catalog = FakePublishedCatalog(_game())
    _wire_public_catalog(monkeypatch, catalog)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [
            await client.get("/api/v1/catalog/games", params={"q": ""}),
            await client.get("/api/v1/catalog/games", params={"q": "a"}),
            await client.get("/api/v1/catalog/games", params={"q": "  "}),
            await client.get("/api/v1/catalog/games", params={"q": "a" * 101}),
        ]

    assert all(response.status_code == 422 for response in responses)
    assert catalog.search_calls == 0


@pytest.mark.anyio
async def test_title_search_treats_special_input_as_data_and_keeps_commerce_facts_separate(
    monkeypatch,
) -> None:
    game = _game(UUID(int=704), title="Sonic the Hedgehog")
    catalog = FakePublishedCatalog(game)
    _wire_public_catalog(monkeypatch, catalog, FakeCommerceReader(unavailable=True))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        special = await client.get(
            "/api/v1/catalog/games", params={"q": "' OR 1=1 --"}
        )
        empty = await client.get(
            "/api/v1/catalog/games", params={"q": "No such title"}
        )
        available_title = await client.get(
            "/api/v1/catalog/games", params={"q": "Sonic"}
        )

    assert special.status_code == empty.status_code == 200
    assert special.json()["items"] == []
    assert empty.json()["items"] == []
    assert available_title.status_code == 200
    assert available_title.json()["commerce_status"] == "unavailable"
    assert len(available_title.json()["items"]) == 1
    assert "offers" not in available_title.json()["items"][0]


def test_postgres_title_search_binds_untrusted_query_and_only_reads_published_games() -> None:
    from contextlib import contextmanager

    class RecordingConnection:
        def __init__(self) -> None:
            self.executions = []

        def execute(self, statement, params=None):
            self.executions.append((str(statement), params or {}))
            return self

        def mappings(self):
            return self

        def __iter__(self):
            return iter(())

    class RecordingEngine:
        def __init__(self) -> None:
            self.connection = RecordingConnection()

        @contextmanager
        def begin(self):
            yield self.connection

    engine = RecordingEngine()
    untrusted = "Sonic' OR 1=1 --"
    hits, next_cursor = PostgresCatalogRepository(engine).search_games(
        query=untrusted, limit=20
    )

    assert hits == []
    assert next_cursor is None
    search_sql, bound_params = next(
        (sql, params)
        for sql, params in engine.connection.executions
        if "WITH search_input AS" in sql
    )
    assert ":query" in search_sql
    assert untrusted not in search_sql
    assert bound_params["query"] == untrusted
    assert "catalog.published_games g" in search_sql
    assert "g.active" in search_sql
    assert "catalog.normalize_title(g.title)" in search_sql
    assert "similarity(normalized_title, normalized_query)" in search_sql
    assert "word_similarity(normalized_query, normalized_title)" in search_sql


def test_postgres_title_search_validates_cursor_for_punctuation_only_query() -> None:
    repository = PostgresCatalogRepository(engine=object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="invalid_cursor"):
        repository.search_games(
            query="!!!",
            limit=20,
            cursor="not-a-valid-cursor",
        )
