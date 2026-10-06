import json
from contextlib import contextmanager
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from app.modules.data_governance.adapters.postgres_repository import PostgresRepository
from app.modules.data_governance.adapters.retroachievements import ALLOWED_CONSOLES


class QueryResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return uuid4()


class Connection:
    def __init__(self, row):
        self.row = row
        self.statements = []
        self.parameters = []

    def execute(self, statement, parameters=None):
        self.statements.append(" ".join(str(statement).split()).lower())
        self.parameters.append(parameters or {})
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
            "console-3-catalog-v1",
            expected_game_count=1,
            expected_batch_count=1,
        )

    assert "mr.publication_right" in engine.connection.statements[0]
    assert len(engine.connection.statements) == 1


@pytest.mark.parametrize(("console_id", "platform"), sorted(ALLOWED_CONSOLES.items()))
def test_private_use_accepts_valid_covers_for_each_allowed_console(
    console_id: int, platform: str
) -> None:
    image = BytesIO()
    Image.new("RGB", (2, 2), "blue").save(image, format="PNG")
    row = {
        "evidence_id": uuid4(),
        "raw_payload": json.dumps({"attributes": {"platform": platform}}),
        "media_path": "cover.png",
        "role": "box_art",
        "storage_right": "confirmed",
        "publication_right": "unknown",
        "attribution": "RetroAchievements",
        "content": image.getvalue(),
    }
    engine = Engine(row)
    repository = RightsRepository(engine)

    result = repository.record_private_use_for_catalog(
        f"console-{console_id}-catalog-v1",
        expected_game_count=1,
        expected_batch_count=1,
        console_id=console_id,
    )

    assert result == {
        "eligible_covers": 1,
        "decisions_created": 1,
        "decisions_existing": 0,
    }
    assert len(engine.connection.statements) == 2
    decision = engine.connection.parameters[1]
    assert decision["scope"] == "loopback_only"
    assert decision["actor"] == "Eduardo"


def test_private_use_rejects_a_catalog_platform_mismatch() -> None:
    image = BytesIO()
    Image.new("RGB", (2, 2), "green").save(image, format="PNG")
    row = {
        "evidence_id": uuid4(),
        "raw_payload": '{"attributes":{"platform":"SNES"}}',
        "media_path": "cover.png",
        "role": "box_art",
        "storage_right": "confirmed",
        "publication_right": "unknown",
        "attribution": "RetroAchievements",
        "content": image.getvalue(),
    }
    engine = Engine(row)
    repository = RightsRepository(engine)

    with pytest.raises(ValueError, match="catalog_platform_mismatch"):
        repository.record_private_use_for_catalog(
            "console-1-catalog-v1",
            expected_game_count=1,
            expected_batch_count=1,
            console_id=1,
        )

    assert len(engine.connection.statements) == 1


def test_private_use_rejects_a_catalog_version_for_another_console() -> None:
    engine = Engine({})
    repository = RightsRepository(engine)

    with pytest.raises(ValueError, match="catalog_console_mismatch"):
        repository.record_private_use_for_catalog(
            "console-2-catalog-v1",
            expected_game_count=1,
            expected_batch_count=1,
            console_id=1,
        )

    assert engine.connection.statements == []


@pytest.mark.parametrize("console_id", [None, 0, 999, 3.0, True])
def test_private_use_rejects_missing_or_unlisted_console_without_queries(
    console_id: object,
) -> None:
    engine = Engine({})
    repository = RightsRepository(engine)

    with pytest.raises(ValueError, match="console_not_allowed"):
        repository.record_private_use_for_catalog(
            "console-3-catalog-v1",
            expected_game_count=1,
            expected_batch_count=1,
            console_id=console_id,  # type: ignore[arg-type]
        )

    assert engine.connection.statements == []
