"""Explicit unit state changes distinguish sale from temporary unavailability."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID


@dataclass(frozen=True)
class UnitAvailabilityChange:
    unit_id: UUID
    state: Literal["available", "unavailable", "sold"]
    expected_state: Literal["available", "unavailable", "sold"]
