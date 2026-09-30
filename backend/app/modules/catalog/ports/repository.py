"""Public ports for Catalog's published projection."""

from typing import Any, Protocol
from uuid import UUID

from app.modules.catalog.domain.publication import PublishedGame


class PublishedCatalog(Protocol):
    def list_games(
        self, *, limit: int, cursor: str | None = None, platform: str | None = None
    ) -> tuple[list[PublishedGame], str | None]: ...

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
