from contextlib import contextmanager
from uuid import uuid4

from app.modules.commerce.adapters.postgres_offers import PostgresOfferReader
from app.modules.commerce.domain.offers import PhysicalUnitFacts


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.rows)


class Connection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _statement, _parameters):
        return Result(self.rows)


class Engine:
    def __init__(self, rows):
        self.connection = Connection(rows)

    @contextmanager
    def connect(self):
        yield self.connection


def test_offer_reader_maps_populated_physical_unit_facts() -> None:
    game_id, offer_id = uuid4(), uuid4()
    reader = PostgresOfferReader(
        Engine(
            [
                {
                    "id": offer_id,
                    "game_id": game_id,
                    "mode": "purchase",
                    "price_minor": 4990,
                    "currency": "BRL",
                    "condition_summary": "Condição comercial",
                    "demo_rank": 1,
                    "sandbox": True,
                    "sku_code": "SKU-1",
                    "available_units": 1,
                    "units": [
                        {
                            "condition_summary": "Muito bom",
                            "defects": ["Risco no estojo"],
                            "included_items": ["Cartucho", "Manual"],
                        }
                    ],
                }
            ]
        )
    )

    offers = reader.list_offers([game_id])

    assert offers[game_id][0].units == (
        PhysicalUnitFacts(
            "Muito bom", ("Risco no estojo",), ("Cartucho", "Manual")
        ),
    )
