"""Public read-only routes for the approved catalog projection."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine

from app.modules.catalog.adapters.postgres_repository import PostgresCatalogRepository
from app.modules.catalog.domain.publication import PublishedGame
from app.platform.config.settings import settings

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])
_engine = create_engine(settings.database_url, pool_pre_ping=True)
_catalog = PostgresCatalogRepository(_engine)


class GameResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    title: str
    platform: str
    attributes: dict[str, Any]
    origin_by_attribute: dict[str, Any]
    source: str
    source_record_id: str
    version: int
    verified_at: str
    cover_attribution: str
    cover_url: str


class GameListResponse(BaseModel):
    items: list[GameResponse]
    next_cursor: str | None


def _game_response(game: PublishedGame) -> GameResponse:
    attributes = game.editorial.get("attributes", {})
    raw_lineage = game.editorial.get("lineage", {})
    public_origin_fields = {
        "source", "source_version", "source_record_id", "captured_at"
    }
    lineage = {
        attribute: {
            key: value
            for key, value in origin.items()
            if key in public_origin_fields
        }
        for attribute, origin in raw_lineage.items()
        if isinstance(attribute, str) and isinstance(origin, dict)
    } if isinstance(raw_lineage, dict) else {}
    return GameResponse(
        id=game.id,
        title=game.title,
        platform=game.platform,
        attributes=attributes,
        origin_by_attribute=lineage,
        source=game.source,
        source_record_id=game.source_record_id,
        version=game.version,
        verified_at=game.verified_at.isoformat(),
        cover_attribution=game.cover_attribution,
        cover_url=f"/api/v1/catalog/games/{game.id}/box-art",
    )


@router.get("/games", response_model=GameListResponse)
async def list_games(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1024),
    platform: str | None = Query(default=None, min_length=1, max_length=100),
) -> GameListResponse:
    try:
        games, next_cursor = _catalog.list_games(
            limit=limit, cursor=cursor, platform=platform
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_cursor") from exc
    return GameListResponse(items=[_game_response(game) for game in games], next_cursor=next_cursor)


@router.get(
    "/games/{game_id}",
    response_model=GameResponse,
    responses={
        304: {"description": "The published game has not changed."},
        404: {"description": "Published game not found."},
    },
)
async def get_game(
    game_id: UUID,
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> GameResponse | Response:
    game = _catalog.get_game(game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="not_found")
    etag = f'"{game.etag}"'
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=60"
    if if_none_match and any(
        token.strip() == "*"
        or token.strip().removeprefix("W/") == etag
        for token in if_none_match.split(",")
    ):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=60"})
    return _game_response(game)


@router.get(
    "/games/{game_id}/box-art",
    response_class=Response,
    responses={404: {"description": "Published box art not found."}},
)
async def get_box_art(game_id: UUID) -> Response:
    cover = _catalog.get_cover(game_id)
    if cover is None:
        raise HTTPException(status_code=404, detail="not_found")
    content, content_type, content_hash = cover
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "ETag": f'"{content_hash}"',
            "Cache-Control": "public, max-age=60",
            "X-Content-Type-Options": "nosniff",
        },
    )
