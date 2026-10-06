from contextlib import contextmanager
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from app.modules.data_governance.adapters.postgres_repository import PostgresRepository


class QueryResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        raise AssertionError("a denied source right must not create a decision")


class Connection:
    def __init__(self, row):
        self.row = row
        self.statements = []

    def execute(self, statement, parameters=None):
        self.statements.append(" ".join(str(statement).split()).lower())
        return QueryResult([self.row])


class Engine:
    def __init__(self, row):
        self.connection = Connection(row)

    @contextmanager
    def begin(self):
        yield self.connection


class RightsRepository(PostgresRepository):
    def catalog_run_ids(
        self,
        catalog_version,
        *,
        expected_game_count,
        expected_batch_count,
        batch_size=None,
    ):
        return [uuid4()]


def test_private_use_does_not_create_decision_for_explicit_denial() -> None:
    image = BytesIO()
    Image.new("RGB", (2, 2), "red").save(image, format="PNG")
    row = {
        "evidence_id": uuid4(),
        "raw_payload": '{"attributes":{"platform":"SNES"}}',
        "media_path": "cover.png",
        "role": "box_art",
        "storage_right": "confirmed",
        "publication_right": "denied",
        "attribution": "RetroAchievements",
        "content": image.getvalue(),
    }
    engine = Engine(row)
    repository = RightsRepository(engine)

    with pytest.raises(ValueError, match="catalog_has_no_valid_covers"):
        repository.record_private_use_for_catalog(
            "catalog-v1", expected_game_count=1, expected_batch_count=1
        )

    assert "mr.publication_right" in engine.connection.statements[0]
    assert len(engine.connection.statements) == 1
