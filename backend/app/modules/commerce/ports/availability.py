from typing import Protocol

from app.modules.commerce.domain.availability import UnitAvailabilityChange


class AvailabilityWriter(Protocol):
    def change(self, change: UnitAvailabilityChange) -> bool: ...
