"""Read-only port for Commerce facts used by public discovery."""

from typing import Protocol
from uuid import UUID

from app.modules.commerce.domain.offers import Offer


class CommerceReadUnavailable(Exception):
    """Commerce could not provide current public offer facts."""


class OfferReader(Protocol):
    def list_offers(self, game_ids: list[UUID]) -> dict[UUID, list[Offer]]: ...
