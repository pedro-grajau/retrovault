"""Public read-only routes for the approved catalog projection."""

from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict

from app.modules.catalog.application.discovery import (
    Availability,
    CommerceUnavailable,
    PublicDiscovery,
    SortOrder,
)
from app.modules.catalog.domain.publication import PublishedGame
from app.modules.catalog.ports.repository import (
    CatalogReadUnavailable,
    PublishedCatalog,
)
from app.modules.commerce.domain.offers import Offer
from app.modules.commerce.ports.offers import OfferReader

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])
_catalog: PublishedCatalog | None = None
_discovery: PublicDiscovery | None = None


def configure_services(catalog: PublishedCatalog, commerce: OfferReader) -> None:
    """Wire module ports from the application composition root."""
    global _catalog, _discovery
    _catalog = catalog
    _discovery = PublicDiscovery(catalog, commerce)


def _catalog_service() -> PublishedCatalog:
    if _catalog is None:
        raise RuntimeError("catalog_services_not_configured")
    return _catalog


def _discovery_service() -> PublicDiscovery:
    if _discovery is None:
        raise RuntimeError("catalog_services_not_configured")
    return _discovery


class OfferResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    sku_code: str
    mode: Literal["purchase", "rental"]
    price_minor: int
    currency: Literal["BRL"]
    condition_summary: str
    available_units: int
    demo_rank: int
    sandbox: bool
    units: list[PhysicalUnitResponse]


class PhysicalUnitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_summary: str
    defects: list[str] | None = None
    included_items: list[str] | None = None


class GameResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    title: str
    platform: str
    attributes: GameAttributes
    origin_by_attribute: dict[str, AttributeOrigin]
    source: str
    source_record_id: str
    version: int
    verified_at: str
    cover_attribution: str
    cover_url: str
    commerce_status: Literal["available", "unavailable"]
    offers: list[OfferResponse] | None = None


class GameAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    platform: str | None = None
    region: str | None = None
    edition: str | None = None
    genre: str | None = None
    developer: str | None = None
    publisher: str | None = None
    year: str | None = None
    release_date: str | None = None
    release_date_granularity: Literal["year", "month", "day"] | None = None
    rating: str | None = None
    description: str | None = None
    included_items: list[str] | None = None


class AttributeOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    source_version: str
    source_record_id: str
    captured_at: str | None = None


class GameListResponse(BaseModel):
    items: list[GameResponse]
    next_cursor: str | None
    commerce_status: Literal["available", "unavailable"]


class GameFacetsResponse(BaseModel):
    platforms: list[str]
    genres: list[str]


def _game_response(
    game: PublishedGame,
    offers: list[Offer] | None = None,
    commerce_status: Literal["available", "unavailable"] = "available",
    *,
    include_unit_facts: bool = False,
) -> GameResponse:
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
        attributes=GameAttributes.model_validate(attributes),
        origin_by_attribute={
            attribute: AttributeOrigin.model_validate(origin)
            for attribute, origin in lineage.items()
        },
        source=game.source,
        source_record_id=game.source_record_id,
        version=game.version,
        verified_at=game.verified_at.isoformat(),
        cover_attribution=game.cover_attribution,
        cover_url=f"/api/v1/catalog/games/{game.id}/box-art",
        commerce_status=commerce_status,
        offers=(
            [
                OfferResponse(
                    id=offer.id,
                    sku_code=offer.sku_code,
                    mode=offer.mode,
                    price_minor=offer.price_minor,
                    currency=offer.currency,
                    condition_summary=offer.condition_summary,
                    available_units=offer.available_units,
                    demo_rank=offer.demo_rank,
                    sandbox=offer.sandbox,
                    units=(
                        [
                            PhysicalUnitResponse(
                                condition_summary=unit.condition_summary,
                                defects=(
                                    list(unit.defects)
                                    if unit.defects is not None
                                    else None
                                ),
                                included_items=(
                                    list(unit.included_items)
                                    if unit.included_items is not None
                                    else None
                                ),
                            )
                            for unit in offer.units
                        ]
                        if include_unit_facts
                        else []
                    ),
                )
                for offer in offers
            ]
            if offers is not None
            else None
        ),
    )


@router.get(
    "/facets",
    response_model=GameFacetsResponse,
    responses={503: {"description": "Catalog unavailable."}},
)
async def list_facets() -> GameFacetsResponse:
    try:
        facets = _catalog_service().list_facets()
    except CatalogReadUnavailable as exc:
        raise HTTPException(status_code=503, detail="catalog_unavailable") from exc
    return GameFacetsResponse(**facets)


@router.get(
    "/games",
    response_model=GameListResponse,
    response_model_exclude_none=True,
    responses={
        503: {"description": "Commerce or Catalog unavailable."},
    },
)
async def list_games(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1024),
    q: str | None = Query(default=None, min_length=2, max_length=100),
    platform: str | None = Query(default=None, min_length=1, max_length=100),
    genre: str | None = Query(default=None, min_length=1, max_length=100),
    availability: Availability | None = Query(default=None),
    sort: SortOrder = Query(default="catalog"),
) -> GameListResponse:
    if q is not None and len(q.strip()) < 2:
        raise HTTPException(status_code=422, detail="invalid_search_query")
    try:
        page = _discovery_service().list_games(
            limit=limit,
            query=q.strip() if q is not None else None,
            cursor=cursor,
            platform=platform,
            genre=genre,
            availability=availability,
            sort=sort,
        )
    except CommerceUnavailable as exc:
        raise HTTPException(status_code=503, detail="commerce_unavailable") from exc
    except CatalogReadUnavailable as exc:
        raise HTTPException(status_code=503, detail="catalog_unavailable") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_cursor") from exc
    return GameListResponse(
        items=[
            _game_response(
                game,
                page.offers_by_game.get(game.id, [])
                if page.offers_by_game is not None
                else None,
                page.commerce_status,
            )
            for game in page.games
        ],
        next_cursor=page.next_cursor,
        commerce_status=page.commerce_status,
    )


@router.get(
    "/games/{game_id}",
    response_model=GameResponse,
    response_model_exclude_none=True,
    responses={
        200: {
            "headers": {
                "ETag": {"description": "Validator for the published game.", "schema": {"type": "string"}},
                "Cache-Control": {"schema": {"type": "string"}},
            }
        },
        304: {
            "description": "The published game has not changed.",
            "headers": {
                "ETag": {"description": "Current game validator.", "schema": {"type": "string"}},
                "Cache-Control": {"schema": {"type": "string"}},
            },
        },
        404: {"description": "Published game not found."},
        503: {"description": "Catalog unavailable."},
    },
)
async def get_game(
    game_id: UUID,
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> GameResponse | Response:
    try:
        game = _catalog_service().get_game(game_id)
    except CatalogReadUnavailable as exc:
        raise HTTPException(status_code=503, detail="catalog_unavailable") from exc
    if game is None:
        raise HTTPException(status_code=404, detail="not_found")
    try:
        offers = _discovery_service().get_game_offers(game_id)
        commerce_status: Literal["available", "unavailable"] = "available"
    except CommerceUnavailable:
        offers = None
        commerce_status = "unavailable"
    game_response = _game_response(
        game, offers, commerce_status, include_unit_facts=True
    )
    etag = f'"{_detail_etag(game.etag, game_response)}"'
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=30"
    if _if_none_match(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=30"})
    return game_response


def _detail_etag(game_etag: str, game: GameResponse) -> str:
    """Include changing Commerce facts in the detail validator."""
    if game.commerce_status == "available" and not game.offers:
        return game_etag
    payload = json.dumps(
        {
            "game_etag": game_etag,
            "commerce_status": game.commerce_status,
            "offers": (
                [offer.model_dump(mode="json") for offer in game.offers]
                if game.offers is not None
                else None
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _if_none_match(value: str | None, etag: str) -> bool:
    return bool(
        value
        and any(
            token.strip() == "*"
            or token.strip().removeprefix("W/") == etag
            for token in value.split(",")
        )
    )


@router.get(
    "/games/{game_id}/box-art",
    response_class=Response,
    responses={
        200: {
            "description": "Authorized published cover image.",
            "content": {
                content_type: {"schema": {"type": "string", "format": "binary"}}
                for content_type in ("image/png", "image/jpeg", "image/webp")
            },
            "headers": {
                "ETag": {"description": "Validator for the image bytes.", "schema": {"type": "string"}},
                "Cache-Control": {"schema": {"type": "string"}},
            },
        },
        304: {
            "description": "The published cover has not changed.",
            "headers": {
                "ETag": {"description": "Current image validator.", "schema": {"type": "string"}},
                "Cache-Control": {"schema": {"type": "string"}},
            },
        },
        404: {"description": "Published box art not found."},
        503: {"description": "Catalog unavailable."},
    },
)
async def get_box_art(
    game_id: UUID,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> Response:
    try:
        cover = _catalog_service().get_cover(game_id)
    except CatalogReadUnavailable as exc:
        raise HTTPException(status_code=503, detail="catalog_unavailable") from exc
    if cover is None:
        raise HTTPException(status_code=404, detail="not_found")
    content, content_type, content_hash = cover
    etag = f'"{content_hash}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=60",
    }
    if _if_none_match(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return Response(
        content=content,
        media_type=content_type,
        headers={
            **headers,
            "X-Content-Type-Options": "nosniff",
        },
    )
