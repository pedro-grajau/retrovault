import json
from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from app.modules.catalog.adapters.postgres_repository import PostgresCatalogRepository
from app.modules.catalog.domain.publication import IdempotencyConflict


class Result:
    def __init__(self, row=None, scalar=None) -> None:
        self.row = row
        self.scalar = scalar

    def mappings(self):
        return self

    def first(self):
        return self.row

    def scalar_one_or_none(self):
        return self.scalar

    def scalar_one(self):
        return self.scalar


class Connection:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.pending: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement, parameters=None):
        sql = str(statement)
        params = dict(parameters or {})
        normalized = " ".join(sql.split()).lower()
        if "select request_hash, response from catalog.command_idempotency" in normalized:
            return Result(row=self.engine.idempotency)
        if "select 1 from data_governance.staging_records" in normalized:
            return Result(row={"exists": 1})
        if "select resulting_etag from data_governance.review_corrections" in normalized:
            return Result(scalar=None)
        if "select id, version, etag, active from catalog.published_games" in normalized:
            return Result(row=self.engine.published_record)
        if normalized.startswith("select * from catalog.published_games"):
            return Result(row=self.engine.game)
        if normalized.startswith(("insert into", "update catalog.published_games")):
            if self.engine.fail_on and self.engine.fail_on in normalized:
                raise RuntimeError("simulated database failure")
            self.pending.append((sql, params))
            if "insert into catalog.command_idempotency" in normalized:
                self.engine.idempotency = {
                    "request_hash": params["request_hash"],
                    "response": json.loads(params["response"]),
                }
        return Result()


class Engine:
    def __init__(
        self, *, fail_on: str | None = None, idempotency=None, game=None,
        published_record=None,
    ) -> None:
        self.fail_on = fail_on
        self.idempotency = idempotency
        self.game = game
        self.published_record = published_record
        self.commits: list[list[tuple[str, dict[str, object]]]] = []
        self.rollbacks = 0

    @contextmanager
    def begin(self):
        connection = Connection(self)
        try:
            yield connection
        except Exception:
            self.rollbacks += 1
            connection.pending.clear()
            raise
        else:
            self.commits.append(connection.pending)


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), "blue").save(output, format="PNG")
    return output.getvalue()


def _candidate() -> dict[str, object]:
    return {
        "run_id": uuid4(),
        "rule_version": "editorial-v1",
        "evidence_id": uuid4(),
        "record_id": "42",
        "source": "retroachievements",
        "source_version": "portfolio-v1",
        "captured_at": datetime(2026, 9, 30, tzinfo=UTC),
        "public_id": uuid4(),
        "state": "review",
        "issues": [],
        "etag": '"review-etag"',
        "values": {"title": "Jogo", "platform": "SNES"},
        "lineage": {"title": {"source": "retroachievements"}},
        "cover": {
            "content": _png(),
            "storage_right": "confirmed",
            "publication_right": "confirmed",
            "attribution": "RetroAchievements",
        },
    }


def _publish(
    repository, candidate, *, key="decision-1", reason="Revisado",
    published_etag=None,
):
    return repository.publish(
        candidate,
        actor="Eduardo",
        reason=reason,
        idempotency_key=key,
        expected_etag='"review-etag"',
        published_etag=published_etag,
    )


def test_publication_writes_projection_audit_outbox_and_idempotency_together() -> None:
    engine = Engine()

    response = _publish(PostgresCatalogRepository(engine), _candidate())

    writes = engine.commits[-1]
    sql = [statement.lower() for statement, _ in writes]
    assert response["state"] == "published"
    assert response["version"] == 1
    assert any("insert into catalog.published_games" in statement for statement in sql)
    assert any("insert into catalog.publication_audit" in statement for statement in sql)
    assert any("insert into platform.outbox_events" in statement for statement in sql)
    assert any("insert into catalog.command_idempotency" in statement for statement in sql)
    assert len(engine.commits) == 2  # Read-only idempotency check, then one write transaction.
    assert engine.commits[0] == []


def test_publication_serializes_lineage_datetimes_as_iso_strings() -> None:
    engine = Engine()
    candidate = _candidate()
    captured_at = datetime(2026, 9, 30, 12, 34, tzinfo=UTC)
    candidate["lineage"] = {"title": {"source": "retroachievements", "captured_at": captured_at}}

    _publish(PostgresCatalogRepository(engine), candidate)

    writes = engine.commits[-1]
    projection = next(params for sql, params in writes if "insert into catalog.published_games" in sql.lower())
    version = next(params for sql, params in writes if "insert into catalog.published_game_versions" in sql.lower())
    expected = captured_at.isoformat()
    assert json.loads(projection["editorial"])["lineage"]["title"]["captured_at"] == expected
    assert json.loads(version["snapshot"])["editorial"]["lineage"]["title"]["captured_at"] == expected


def test_publication_rejects_retired_game_reactivation() -> None:
    engine = Engine(
        published_record={
            "id": uuid4(),
            "version": 4,
            "etag": "retired-etag",
            "active": False,
        }
    )

    with pytest.raises(ValueError, match="retired_game_cannot_be_republished"):
        _publish(
            PostgresCatalogRepository(engine), _candidate(),
            published_etag='"retired-etag"',
        )

    assert engine.rollbacks == 1
    assert all(not transaction for transaction in engine.commits)


def test_publication_replays_original_result_and_rejects_key_reuse() -> None:
    candidate = _candidate()
    repository = PostgresCatalogRepository(Engine())
    request_hash = repository._request_hash(
        "publish",
        {
            "candidate_etag": '"review-etag"',
            "source": "retroachievements",
            "source_record_id": "42",
            "reason": "Revisado",
            "actor": "Eduardo",
        },
    )
    response = {"game_id": "stable-id", "version": 2, "etag": "abc", "state": "published"}
    engine = Engine(idempotency={"request_hash": request_hash, "response": response})
    repository = PostgresCatalogRepository(engine)
    candidate["state"] = "quarantine"
    candidate["issues"] = [{"code": "cover_missing"}]

    assert _publish(repository, candidate) == response
    assert engine.commits == [[]]

    with pytest.raises(IdempotencyConflict, match="idempotency_key_reused"):
        _publish(repository, candidate, reason="Outro conteúdo")
    assert engine.rollbacks == 1


def test_publication_failure_rolls_back_projection_audit_and_outbox() -> None:
    engine = Engine(fail_on="insert into platform.outbox_events")

    with pytest.raises(RuntimeError, match="simulated database failure"):
        _publish(PostgresCatalogRepository(engine), _candidate())

    assert engine.rollbacks == 1
    assert all(not transaction for transaction in engine.commits)


def test_retirement_versions_projection_audit_and_outbox_idempotently() -> None:
    game_id = uuid4()
    engine = Engine(
        game={
            "id": game_id,
            "version": 3,
            "etag": "current-etag",
            "active": True,
            "source": "retroachievements",
            "source_record_id": "42",
            "editorial": {"attributes": {"title": "Jogo", "platform": "SNES"}},
            "cover_hash": "a" * 64,
        }
    )
    repository = PostgresCatalogRepository(engine)
    decision = {
        "actor": "Eduardo",
        "reason": "Retirada editorial",
        "idempotency_key": "retire-1",
        "expected_etag": '"current-etag"',
    }

    first = repository.retire(game_id, **decision)
    second = repository.retire(game_id, **decision)

    assert first == second
    assert first["state"] == "retired"
    assert first["version"] == 4
    writes = [statement.lower() for statement, _ in engine.commits[0]]
    assert any("update catalog.published_games" in statement for statement in writes)
    assert any("insert into catalog.published_game_versions" in statement for statement in writes)
    assert any("insert into catalog.publication_audit" in statement for statement in writes)
    assert any("insert into platform.outbox_events" in statement for statement in writes)
    assert len(engine.commits[1]) == 0
