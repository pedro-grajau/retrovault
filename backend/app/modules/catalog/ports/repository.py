"""Public ports for Catalog's published projection."""

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from app.modules.catalog.domain.publication import PublishedGame


class CatalogReadUnavailable(Exception):
    """The published Catalog projection could not be read."""


@dataclass(frozen=True)
class PublishedSearchHit:
    """A published game and the search keyset position immediately after it."""

    game: PublishedGame
    cursor_after: str


class PublishedCatalog(Protocol):
    def list_facets(self) -> dict[str, list[str]]: ...

    def list_games(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        platform: str | None = None,
        genre: str | None = None,
    ) -> tuple[list[PublishedGame], str | None]: ...

    def search_games(
        self,
        *,
        query: str,
        limit: int,
        cursor: str | None = None,
        platform: str | None = None,
        genre: str | None = None,
        cursor_context: str | None = None,
    ) -> tuple[list[PublishedSearchHit], str | None]: ...

    def encode_cursor(self, game: PublishedGame) -> str: ...

    def get_game(self, game_id: UUID) -> PublishedGame | None: ...

    def get_cover(self, game_id: UUID) -> tuple[bytes, str, str] | None: ...

    def publish(
        self,
        candidate: dict[str, Any],
        *,
        actor: str,
        reason: str,
        idempotency_key: str,
        expected_etag: str,
    ) -> dict[str, Any]: ...

    def retire(
        self,
        game_id: UUID,
        *,
        actor: str,
        reason: str,
        idempotency_key: str,
        expected_etag: str,
    ) -> dict[str, Any]: ...


class CatalogPublicationRepository(Protocol):
    """Write-side port consumed by the application publisher."""

    def current_etag(
        self, source: str, source_record_id: str, connection: Any | None = None
    ) -> str | None: ...

    def publish(
        self,
        candidate: dict[str, Any],
        *,
        actor: str,
        reason: str,
        idempotency_key: str,
        expected_etag: str,
        published_etag: str | None = None,
        connection: Any | None = None,
    ) -> dict[str, Any]: ...

    def retire(
        self,
        game_id: UUID,
        *,
        actor: str,
        reason: str,
        idempotency_key: str,
        expected_etag: str,
        connection: Any | None = None,
    ) -> dict[str, Any]: ...

    def withdraw(
        self,
        game_id: UUID,
        *,
        actor: str,
        reason: str,
        idempotency_key: str,
        expected_etag: str,
        connection: Any | None = None,
    ) -> dict[str, Any]: ...
