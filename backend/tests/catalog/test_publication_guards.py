import base64
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.modules.catalog.adapters.postgres_repository import PostgresCatalogRepository
from app.modules.catalog.domain.publication import PublicationConflict
from app.modules.catalog.ports.repository import CatalogReadUnavailable


class NoReplayCatalogRepository(PostgresCatalogRepository):
    def _idempotent_result(
        self,
        idempotency_key: str,
        request_hash: str,
        connection: Connection | None,
    ) -> dict[str, object] | None:
        return None


class ReplayedCatalogRepository(PostgresCatalogRepository):
    def _idempotent_result(
        self,
        idempotency_key: str,
        request_hash: str,
        connection: Connection | None,
    ) -> dict[str, object] | None:
        return {"game_id": "stable-id", "version": 1, "state": "published"}


class FailedReadEngine:
    def connect(self):
        raise SQLAlchemyError("database unavailable")

    def begin(self):
        raise SQLAlchemyError("database unavailable")


def _candidate(**overrides):
    candidate = {
        "run_id": uuid4(),
        "rule_version": "editorial-v1",
        "evidence_id": uuid4(),
        "record_id": "42",
        "source": "retroachievements",
        "state": "review",
        "issues": [],
        "etag": '"candidate-etag"',
        "values": {"title": "Jogo", "platform": "SNES"},
        "cover": {
            "content": b"invalid image bytes",
            "storage_right": "confirmed",
            "publication_right": "confirmed",
            "attribution": "RetroAchievements",
        },
    }
    candidate.update(overrides)
    return candidate


def _publish(candidate):
    repository = NoReplayCatalogRepository(engine=None)
    return repository.publish(
        candidate,
        actor="Eduardo",
        reason="Revisão editorial concluída",
        idempotency_key="decision-1",
        expected_etag='"candidate-etag"',
    )


def test_publication_rejects_quarantined_candidate_before_persistence() -> None:
    candidate = _candidate(state="quarantine", issues=[{"code": "cover_missing"}])

    with pytest.raises(PublicationConflict, match="candidate_not_eligible"):
        _publish(candidate)


def test_publication_requires_confirmed_cover_rights_and_attribution() -> None:
    candidate = _candidate(
        cover={
            "content": b"bytes",
            "storage_right": "confirmed",
            "publication_right": "unknown",
            "attribution": "",
        }
    )

    with pytest.raises(PublicationConflict, match="candidate_etag_or_cover_invalid"):
        _publish(candidate)


def test_private_use_decision_cannot_override_explicit_publication_denial() -> None:
    candidate = _candidate(
        cover={
            "content": b"bytes",
            "storage_right": "confirmed",
            "publication_right": "confirmed",
            "source_publication_right": "denied",
            "rights_decision_id": uuid4(),
            "rights_scope": "loopback_only",
            "attribution": "RetroAchievements",
        }
    )

    with pytest.raises(PublicationConflict, match="candidate_etag_or_cover_invalid"):
        _publish(candidate)


def test_publication_rejects_commercial_fields() -> None:
    candidate = _candidate(values={"title": "Jogo", "platform": "SNES", "price": "99.90"})

    with pytest.raises(ValueError, match="invalid_editorial_fields"):
        _publish(candidate)


def test_publication_rejects_stale_review_etag() -> None:
    candidate = _candidate()
    repository = NoReplayCatalogRepository(engine=None)

    with pytest.raises(PublicationConflict, match="etag_conflict"):
        repository.publish(
            candidate,
            actor="Eduardo",
            reason="Revisão editorial concluída",
            idempotency_key="decision-1",
            expected_etag='"stale-etag"',
        )


def test_publication_replays_before_revalidating_changed_candidate() -> None:
    candidate = _candidate(state="quarantine", issues=[{"code": "cover_missing"}])
    repository = ReplayedCatalogRepository(engine=None)

    response = repository.publish(
        candidate,
        actor="Eduardo",
        reason="Revisão editorial concluída",
        idempotency_key="decision-1",
        expected_etag='"candidate-etag"',
    )

    assert response == {"game_id": "stable-id", "version": 1, "state": "published"}


def test_catalog_cursor_round_trip_and_rejects_malformed_values() -> None:
    game_id = uuid4()
    cursor = PostgresCatalogRepository._cursor_encode(game_id)

    assert PostgresCatalogRepository._cursor_decode(cursor) == game_id
    invalid = [
        "***not-a-cursor***",
        base64.urlsafe_b64encode(
            json.dumps(["\x00", str(game_id)]).encode()
        ).decode().rstrip("="),
    ]
    for value in invalid:
        with pytest.raises(ValueError, match="invalid_cursor"):
            PostgresCatalogRepository._cursor_decode(value)


def test_all_catalog_read_adapters_translate_database_failures() -> None:
    repository = PostgresCatalogRepository(FailedReadEngine())
    reads = (
        repository.list_facets,
        lambda: repository.list_games(limit=10),
        lambda: repository.search_games(query="jogo", limit=10),
        lambda: repository.get_game(uuid4()),
        lambda: repository.get_cover(uuid4()),
    )

    for read in reads:
        with pytest.raises(CatalogReadUnavailable):
            read()


class ReadResult:
    def mappings(self):
        return self

    def __iter__(self):
        return iter(())

    def first(self):
        return None


class ReadConnection:
    def __init__(self, queries):
        self.queries = queries

    def execute(self, statement, parameters=None):
        self.queries.append((" ".join(str(statement).split()), dict(parameters or {})))
        return ReadResult()


class ReadEngine:
    def __init__(self):
        self.queries = []

    @contextmanager
    def connect(self):
        yield ReadConnection(self.queries)


class FailedQueryConnection:
    def execute(self, statement, parameters=None):
        raise SQLAlchemyError("query unavailable")


class FailedQueryEngine:
    @contextmanager
    def connect(self):
        yield FailedQueryConnection()


def test_catalog_read_adapter_translates_query_execution_failures() -> None:
    repository = PostgresCatalogRepository(FailedQueryEngine())

    with pytest.raises(CatalogReadUnavailable):
        repository.list_facets()


def test_catalog_pagination_uses_immutable_uuid_key() -> None:
    engine = ReadEngine()
    game_id = uuid4()
    repository = PostgresCatalogRepository(engine)

    games, cursor = repository.list_games(
        limit=20, cursor=repository._cursor_encode(game_id)
    )

    query, parameters = engine.queries[0]
    assert games == []
    assert cursor is None
    assert "g.id > :last_id" in query
    assert "ORDER BY g.id" in query
    assert "last_title" not in query
    assert parameters["last_id"] == game_id


def test_inactive_public_games_are_omitted_from_all_repository_reads() -> None:
    engine = ReadEngine()
    repository = PostgresCatalogRepository(engine)
    game_id = uuid4()

    games, _ = repository.list_games(limit=10)
    detail = repository.get_game(game_id)
    cover = repository.get_cover(game_id)

    assert games == []
    assert detail is None
    assert cover is None
    assert len(engine.queries) == 3
    assert all("g.active" in query.lower() for query, _ in engine.queries)


class PageResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.rows)


class PageConnection:
    def __init__(self, engine):
        self.engine = engine

    def execute(self, statement, parameters=None):
        params = dict(parameters or {})
        self.engine.queries.append((" ".join(str(statement).split()), params))
        if "last_id" not in params:
            return PageResult(self.engine.first_page)
        return PageResult(self.engine.second_page)


class PageEngine:
    def __init__(self, first_page, second_page):
        self.first_page = first_page
        self.second_page = second_page
        self.queries = []

    @contextmanager
    def connect(self):
        yield PageConnection(self)


def _page_row(game_id):
    return {
        "id": game_id,
        "title": f"Jogo {game_id}",
        "platform": "SNES",
        "editorial": {"attributes": {"title": f"Jogo {game_id}"}},
        "source": "retroachievements",
        "source_record_id": str(game_id),
        "version": 1,
        "etag": "a" * 64,
        "verified_at": datetime(2026, 9, 30, tzinfo=UTC),
        "cover_hash": "b" * 64,
        "content_type": "image/png",
        "attribution": "RetroAchievements",
    }


def test_catalog_pagination_continues_without_duplicates_across_two_pages() -> None:
    ids = sorted([uuid4(), uuid4(), uuid4()])
    engine = PageEngine(
        [_page_row(game_id) for game_id in ids],
        [_page_row(ids[2])],
    )
    repository = PostgresCatalogRepository(engine)

    first_page, cursor = repository.list_games(limit=2)
    second_page, next_cursor = repository.list_games(limit=2, cursor=cursor)
    collected = [game.id for game in [*first_page, *second_page]]

    assert [game.id for game in first_page] == ids[:2]
    assert cursor == repository._cursor_encode(ids[1])
    assert [game.id for game in second_page] == [ids[2]]
    assert next_cursor is None
    assert len(collected) == len(set(collected)) == 3
    assert engine.queries[1][1]["last_id"] == ids[1]
