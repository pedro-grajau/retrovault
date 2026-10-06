"""Application boundary for atomic publication commands."""

from typing import Any
from uuid import UUID

from app.modules.catalog.ports.repository import CatalogPublicationRepository


class CatalogPublisher:
    """Application service that delegates commands through the write-side port."""

    def __init__(self, repository: CatalogPublicationRepository) -> None:
        self.repository = repository

    def current_etag(
        self, source: str, source_record_id: str, connection: Any | None = None
    ) -> str | None:
        return self.repository.current_etag(source, source_record_id, connection)

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
    ) -> dict[str, Any]:
        return self.repository.publish(
            candidate,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            expected_etag=expected_etag,
            published_etag=published_etag,
            connection=connection,
        )

    def retire(
        self,
        game_id: UUID,
        *,
        actor: str,
        reason: str,
        idempotency_key: str,
        expected_etag: str,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        return self.repository.retire(
            game_id,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            expected_etag=expected_etag,
            connection=connection,
        )

    def withdraw(
        self,
        game_id: UUID,
        *,
        actor: str,
        reason: str,
        idempotency_key: str,
        expected_etag: str,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        return self.repository.withdraw(
            game_id,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            expected_etag=expected_etag,
            connection=connection,
        )
