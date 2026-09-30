"""Tipos do catálogo publicado; sem dependências de framework ou persistência."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class PublishedGame:
    id: UUID
    title: str
    platform: str
    editorial: dict[str, Any]
    source: str
    source_record_id: str
    version: int
    etag: str
    verified_at: datetime
    cover_hash: str
    cover_content_type: str
    cover_attribution: str


class PublicationConflict(ValueError):
    """The candidate or published projection changed since review."""


class IdempotencyConflict(ValueError):
    """An idempotency key was reused with a different request."""


class GameNotFound(LookupError):
    """No currently published game has this public identifier."""
