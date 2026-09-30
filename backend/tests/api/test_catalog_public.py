from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.main import app
from app.modules.catalog.adapters.postgres_repository import PostgresCatalogRepository
from app.modules.catalog.api import router as catalog_router
from app.modules.catalog.domain.publication import PublishedGame


class FakePublishedCatalog:
    def __init__(self, game: PublishedGame | None) -> None:
        self.game = game

    def list_games(self, *, limit: int, cursor=None, platform=None):
        PostgresCatalogRepository._cursor_decode(cursor)
        games = [self.game] if self.game is not None else []
        return games[:limit], None

    def get_game(self, game_id):
        return self.game if self.game is not None and self.game.id == game_id else None

    def get_cover(self, game_id):
        if self.game is None or self.game.id != game_id:
            return None
        return b"published-cover", "image/png", self.game.cover_hash


def _game() -> PublishedGame:
    return PublishedGame(
        id=uuid4(),
        title="Jogo publicado",
        platform="SNES",
        editorial={
            "attributes": {"title": "Jogo publicado", "platform": "SNES"},
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


@pytest.mark.anyio
async def test_public_catalog_exposes_only_published_projection(monkeypatch) -> None:
    game = _game()
    monkeypatch.setattr(catalog_router, "_catalog", FakePublishedCatalog(game))
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
        missing = await client.get(f"/api/v1/catalog/games/{uuid4()}/box-art")

    assert response.status_code == 200
    assert response.content == b"published-cover"
    assert response.headers["Content-Type"] == "image/png"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "public, max-age=60"
    assert missing.status_code == 404


@pytest.mark.anyio
async def test_public_list_rejects_invalid_cursor(monkeypatch) -> None:
    monkeypatch.setattr(catalog_router, "_catalog", FakePublishedCatalog(_game()))
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

    assert "304" in detail
    assert "404" in detail
    assert "404" in box_art
