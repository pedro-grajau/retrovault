"""Compare-and-set unit updates; the database trigger emits the event atomically."""

from sqlalchemy import Engine, text

from app.modules.commerce.domain.availability import UnitAvailabilityChange


class PostgresAvailabilityWriter:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def change(self, change: UnitAvailabilityChange) -> bool:
        if change.state not in {
            "available",
            "unavailable",
            "sold",
        } or change.expected_state not in {"available", "unavailable", "sold"}:
            raise ValueError("invalid_unit_state")
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE commerce.physical_units SET state=:state WHERE id=:id AND state=:expected"
                ),
                {
                    "state": change.state,
                    "id": change.unit_id,
                    "expected": change.expected_state,
                },
            )
            return result.rowcount == 1
