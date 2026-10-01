"""Commerce facts exposed to public catalog discovery."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID


@dataclass(frozen=True)
class Offer:
    id: UUID
    game_id: UUID
    mode: Literal["purchase", "rental"]
    price_minor: int
    currency: str
    condition_summary: str
    available_units: int
    demo_rank: int
    sandbox: bool
