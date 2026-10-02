"""Public discovery orchestration across the Catalog and Commerce authorities."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from app.modules.catalog.domain.publication import PublishedGame
from app.modules.catalog.ports.repository import PublishedCatalog, PublishedSearchHit
from app.modules.commerce.domain.offers import Offer
from app.modules.commerce.ports.offers import CommerceReadUnavailable, OfferReader

Availability = Literal["available", "unavailable"]
SortOrder = Literal["catalog", "demo_popular"]


@dataclass(frozen=True)
class DiscoveryPage:
    games: list[PublishedGame]
    offers_by_game: dict[UUID, list[Offer]] | None
    next_cursor: str | None
    commerce_status: Literal["available", "unavailable"]


class CommerceUnavailable(Exception):
    """Commerce facts could not be read, so an availability filter cannot run."""


class PublicDiscovery:
    def __init__(self, catalog: PublishedCatalog, commerce: OfferReader) -> None:
        self.catalog = catalog
        self.commerce = commerce

    @staticmethod
    def _is_available(offers: list[Offer]) -> bool:
        return any(offer.available_units > 0 for offer in offers)

    @staticmethod
    def _demo_cursor_decode(cursor: str | None) -> int:
        if cursor is None:
            return 0
        if not cursor or len(cursor) > 128:
            raise ValueError("invalid_cursor")
        try:
            decoded = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            )
            value = json.loads(decoded)
            offset = value["demo_popular"]
            if type(offset) is not int or offset < 0:
                raise ValueError
            return offset
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error) as exc:
            raise ValueError("invalid_cursor") from exc

    @staticmethod
    def _demo_cursor_encode(offset: int) -> str:
        value = json.dumps({"demo_popular": offset}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    def list_games(
        self,
        *,
        limit: int,
        query: str | None = None,
        cursor: str | None = None,
        platform: str | None = None,
        genre: str | None = None,
        availability: Availability | None = None,
        sort: SortOrder = "catalog",
    ) -> DiscoveryPage:
        if query is not None:
            return self._search_games(
                query=query,
                limit=limit,
                cursor=cursor,
                platform=platform,
                genre=genre,
                availability=availability,
            )
        if sort == "demo_popular":
            return self._list_demo_popular(
                limit=limit,
                cursor=cursor,
                platform=platform,
                genre=genre,
                availability=availability,
            )
        if availability is None:
            games, next_cursor = self.catalog.list_games(
                limit=limit,
                cursor=cursor,
                platform=platform,
                genre=genre,
            )
            try:
                offers = self.commerce.list_offers([game.id for game in games])
            except CommerceReadUnavailable:
                return DiscoveryPage(games, None, next_cursor, "unavailable")
            return DiscoveryPage(games, offers, next_cursor, "available")
        return self._list_filtered_availability(
            limit=limit,
            cursor=cursor,
            platform=platform,
            genre=genre,
            availability=availability,
        )

    def _search_games(
        self,
        *,
        query: str,
        limit: int,
        cursor: str | None,
        platform: str | None,
        genre: str | None,
        availability: Availability | None,
    ) -> DiscoveryPage:
        if len(query.strip()) < 2 or len(query) > 100:
            raise ValueError("invalid_search_query")
        if availability is None:
            hits, next_cursor = self.catalog.search_games(
                query=query,
                limit=limit,
                cursor=cursor,
                platform=platform,
                genre=genre,
                cursor_context=availability,
            )
            games = [hit.game for hit in hits]
            try:
                offers = self.commerce.list_offers([game.id for game in games])
            except CommerceReadUnavailable:
                return DiscoveryPage(games, None, next_cursor, "unavailable")
            return DiscoveryPage(games, offers, next_cursor, "available")

        results: list[tuple[PublishedSearchHit, list[Offer]]] = []
        scan_cursor = cursor
        exhausted = False
        while len(results) <= limit and not exhausted:
            hits, next_cursor = self.catalog.search_games(
                query=query,
                limit=100,
                cursor=scan_cursor,
                platform=platform,
                genre=genre,
                cursor_context=availability,
            )
            if not hits:
                break
            try:
                offers_by_game = self.commerce.list_offers(
                    [hit.game.id for hit in hits]
                )
            except CommerceReadUnavailable as exc:
                raise CommerceUnavailable from exc
            for hit in hits:
                offers = offers_by_game.get(hit.game.id, [])
                if self._is_available(offers) == (availability == "available"):
                    results.append((hit, offers))
                    if len(results) > limit:
                        break
            exhausted = next_cursor is None
            scan_cursor = next_cursor

        has_more = len(results) > limit
        page = results[:limit]
        games = [hit.game for hit, _ in page]
        offers = {hit.game.id: current for hit, current in page}
        next_page_cursor = page[-1][0].cursor_after if has_more and page else None
        return DiscoveryPage(games, offers, next_page_cursor, "available")

    def _list_filtered_availability(
        self,
        *,
        limit: int,
        cursor: str | None,
        platform: str | None,
        genre: str | None,
        availability: Availability,
    ) -> DiscoveryPage:
        results: list[PublishedGame] = []
        result_offers: dict[UUID, list[Offer]] = {}
        scan_cursor = cursor
        exhausted = False
        while len(results) <= limit and not exhausted:
            games, next_cursor = self.catalog.list_games(
                limit=100,
                cursor=scan_cursor,
                platform=platform,
                genre=genre,
            )
            if not games:
                break
            try:
                offers = self.commerce.list_offers([game.id for game in games])
            except CommerceReadUnavailable as exc:
                raise CommerceUnavailable from exc
            for game in games:
                current = offers.get(game.id, [])
                is_available = self._is_available(current)
                if is_available == (availability == "available"):
                    results.append(game)
                    result_offers[game.id] = current
                    if len(results) > limit:
                        break
            exhausted = next_cursor is None
            scan_cursor = next_cursor

        has_more = len(results) > limit
        page_games = results[:limit]
        page_offers = {game.id: result_offers[game.id] for game in page_games}
        next_page_cursor = (
            self.catalog.encode_cursor(page_games[-1]) if has_more and page_games else None
        )
        return DiscoveryPage(page_games, page_offers, next_page_cursor, "available")

    def _list_demo_popular(
        self,
        *,
        limit: int,
        cursor: str | None,
        platform: str | None,
        genre: str | None,
        availability: Availability | None,
    ) -> DiscoveryPage:
        offset = self._demo_cursor_decode(cursor)
        all_games: list[PublishedGame] = []
        all_offers: dict[UUID, list[Offer]] = {}
        scan_cursor: str | None = None
        while True:
            games, next_cursor = self.catalog.list_games(
                limit=100,
                cursor=scan_cursor,
                platform=platform,
                genre=genre,
            )
            if not games:
                break
            try:
                offers = self.commerce.list_offers([game.id for game in games])
            except CommerceReadUnavailable as exc:
                raise CommerceUnavailable from exc
            for game in games:
                current = offers.get(game.id, [])
                if availability == "available" and not self._is_available(current):
                    continue
                if availability == "unavailable" and self._is_available(current):
                    continue
                all_games.append(game)
                all_offers[game.id] = current
            if next_cursor is None:
                break
            scan_cursor = next_cursor
        ranked = sorted(
            all_games,
            key=lambda game: (
                min(
                    (offer.demo_rank for offer in all_offers[game.id]),
                    default=2**31,
                ),
                -len(
                    {
                        offer.mode
                        for offer in all_offers[game.id]
                    }
                ),
                str(game.id),
            ),
        )

        page_games = ranked[offset : offset + limit]
        next_offset = offset + len(page_games)
        next_cursor = (
            self._demo_cursor_encode(next_offset) if next_offset < len(ranked) else None
        )
        return DiscoveryPage(
            page_games,
            {game.id: all_offers[game.id] for game in page_games},
            next_cursor,
            "available",
        )
