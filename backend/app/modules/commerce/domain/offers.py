"""Commerce facts exposed to public catalog discovery."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID


@dataclass(frozen=True)
class PhysicalUnitFacts:
    """Current, customer-facing facts recorded for one available unit."""

    condition_summary: str
    defects: tuple[str, ...] | None
    included_items: tuple[str, ...] | None


@dataclass(frozen=True)
class Offer:
    id: UUID
    game_id: UUID
    mode: Literal["purchase", "rental"]
    price_minor: int
    currency: Literal["BRL"]
    condition_summary: str
    available_units: int
    demo_rank: int
    sandbox: bool
    sku_code: str = ""
    units: tuple[PhysicalUnitFacts, ...] = ()
