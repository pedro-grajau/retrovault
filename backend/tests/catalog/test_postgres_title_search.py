"""Real PostgreSQL coverage for Catalog title search.

Run only against the disposable database created by ``scripts/compose-smoke.sh``.
The fixture keeps inserts inside one transaction and rolls them back afterward.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import create_engine, text

from app.modules.catalog.adapters.postgres_repository import PostgresCatalogRepository
from app.platform.config.settings import settings


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="PostgreSQL efêmero do compose não está ativo",
)
def test_postgres_title_search_ranks_pages_and_stays_under_latency_budget() -> None:
    engine = create_engine(settings.database_url)
    connection = engine.connect()
    transaction = connection.begin()

    @contextmanager
    def use_seeded_transaction():
        yield connection

    class TransactionalEngine:
        begin = staticmethod(use_seeded_transaction)

    try:
        run_id = uuid4().hex
        scenario_platform = f"story-1.6-{run_id}"
        media_hash = hashlib.sha256(run_id.encode()).hexdigest()
        connection.execute(
            text("""
                INSERT INTO catalog.published_media
                    (content_hash, content, content_type, attribution, rights_reference, created_at)
                VALUES (:hash, :content, 'image/png', 'Story 1.6 test', 'test-only', :created_at)
            """),
            {
                "hash": media_hash,
                "content": b"story-1.6-test-image",
                "created_at": datetime.now(UTC),
            },
        )

        rows: list[dict[str, object]] = []
        scenario_titles = [
            ("Super Mario World", scenario_platform, "Demo"),
            ("Super Mario World", scenario_platform, "Demo"),
            ("Super Mario World 2", scenario_platform, "Demo"),
            ("Super Mario Wurld", scenario_platform, "Demo"),
            ("The Legend of Zelda A Link to the Past", scenario_platform, "Demo"),
        ]
        filter_platform = f"story-1.6-filter-{run_id}"
        filter_titles = [
            ("Filter Sentinel", scenario_platform, "Demo"),
            ("Filter Sentinel", scenario_platform, "Action"),
            ("Filter Sentinel", filter_platform, "Demo"),
        ]
        editorial_titles = [
            ("Purple Metadata Item", scenario_platform, "Demo"),
            ("Orange Editorial Item", scenario_platform, "Demo"),
        ]
        titles = scenario_titles + filter_titles + editorial_titles + [
            (
                f"Demo RetroVault Game {index:05d}",
                "SNES" if index % 2 else "PS2",
                "Action" if index % 4 == 0 else "Demo",
            )
            for index in range(1, 2501)
        ]
        ids: list[UUID] = []
        for index, (title, platform, genre) in enumerate(titles):
            game_id = uuid5(NAMESPACE_URL, f"retrovault-story-1.6:{run_id}:{index}")
            ids.append(game_id)
            rows.append(
                {
                    "id": game_id,
                    "source_record_id": f"story-1.6:{run_id}:{index}",
                    "title": title,
                    "platform": platform,
                    "editorial": json.dumps({"attributes": {
                        "title": title,
                        "platform": platform,
                        "genre": genre,
                        **(
                            {"description": "Super Mario Wurldd"}
                            if title == "Purple Metadata Item"
                            else {"description": "handheld puzzle magic"}
                            if title == "Orange Editorial Item"
                            else {}
                        ),
                    }}),
                    "etag": "a" * 64,
                    "cover_hash": media_hash,
                    "updated_at": datetime.now(UTC),
                }
            )
        connection.execute(
            text("""
                INSERT INTO catalog.published_games
                    (id, source, source_record_id, title, platform, editorial, version,
                     etag, active, cover_hash, updated_at)
                VALUES
                    (:id, 'retroachievements', :source_record_id, :title, :platform,
                     CAST(:editorial AS jsonb), 1, :etag, true, :cover_hash, :updated_at)
            """),
            rows,
        )

        repository = PostgresCatalogRepository(TransactionalEngine())
        scenario_ids = ids[:4]
        expected = [
            *sorted(scenario_ids[:2]),
            scenario_ids[2],
            scenario_ids[3],
        ]
        actual: list[UUID] = []
        cursor = None
        pages = 0
        while True:
            hits, cursor = repository.search_games(
                query="Super Mario World",
                limit=1,
                cursor=cursor,
                platform=scenario_platform,
            )
            if not hits:
                break
            actual.extend(hit.game.id for hit in hits)
            pages += 1
            assert pages <= len(expected), (
                "a busca de ordenação retornou mais páginas que títulos semeados; "
                f"ids={actual}; cursor="
                f"{PostgresCatalogRepository._search_cursor_decode(cursor)}"
            )
            if cursor is None:
                break
        assert actual == expected

        long_title_hits, _ = repository.search_games(
            query="Zelad",
            limit=5,
            platform=scenario_platform,
        )
        assert [hit.game.title for hit in long_title_hits] == [
            "The Legend of Zelda A Link to the Past"
        ]

        editorial_only_hits, _ = repository.search_games(
            query="handheld puzzle magic", limit=5, platform=scenario_platform
        )
        assert [hit.game.title for hit in editorial_only_hits] == [
            "Orange Editorial Item"
        ]
        assert editorial_only_hits[0].matched_fields == ("description",)

        fuzzy_and_editorial_hits, _ = repository.search_games(
            query="Super Mario Wurldd", limit=10, platform=scenario_platform
        )
        fuzzy_title_index = next(
            index
            for index, hit in enumerate(fuzzy_and_editorial_hits)
            if hit.game.title == "Super Mario Wurld"
        )
        editorial_only_index = next(
            index
            for index, hit in enumerate(fuzzy_and_editorial_hits)
            if hit.game.title == "Purple Metadata Item"
        )
        assert fuzzy_title_index < editorial_only_index
        assert "title" in fuzzy_and_editorial_hits[fuzzy_title_index].matched_fields
        assert fuzzy_and_editorial_hits[editorial_only_index].matched_fields == (
            "description",
        )

        filter_ids = ids[5:8]
        by_platform, _ = repository.search_games(
            query="Filter Sentinel", limit=20, platform=scenario_platform
        )
        by_genre, _ = repository.search_games(
            query="Filter Sentinel", limit=20, genre="Demo"
        )
        by_both, _ = repository.search_games(
            query="Filter Sentinel",
            limit=20,
            platform=scenario_platform,
            genre="Demo",
        )
        assert {hit.game.id for hit in by_platform} == set(filter_ids[:2]), (
            [(hit.game.title, str(hit.game.id)) for hit in by_platform],
            [str(game_id) for game_id in filter_ids[:2]],
        )
        assert {hit.game.id for hit in by_genre} == {filter_ids[0], filter_ids[2]}
        assert [hit.game.id for hit in by_both] == [filter_ids[0]]

        filtered_hits, _ = repository.search_games(
            query="Demo RetroVault Game",
            limit=20,
            platform="SNES",
            genre="Demo",
        )
        assert filtered_hits
        assert all(hit.game.platform == "SNES" for hit in filtered_hits)
        assert all(
            hit.game.editorial["attributes"]["genre"] == "Demo"
            for hit in filtered_hits
        )

        workloads = {
            "exact": {"query": "Demo RetroVault Game 00001"},
            "fuzzy": {"query": "Demo RetroVault Game 00O01"},
            "filtered": {
                "query": "Demo RetroVault Game",
                "platform": "SNES",
                "genre": "Demo",
            },
            "empty": {"query": "zzqvxjplmnoq"},
        }
        for workload in workloads.values():
            for _ in range(4):
                repository.search_games(limit=20, **workload)

        timings: dict[str, float] = {}
        for name, workload in workloads.items():
            samples = []
            for _ in range(25):
                start = time.perf_counter()
                repository.search_games(limit=20, **workload)
                samples.append((time.perf_counter() - start) * 1000)
            samples.sort()
            timings[name] = samples[math.ceil(len(samples) * 0.95) - 1]

        logging.getLogger(__name__).warning(
            "Story 1.6 seeded PostgreSQL warmed p95 (ms): %s",
            ", ".join(f"{name}={value:.1f}" for name, value in timings.items()),
        )
        assert all(value < 500 for value in timings.values())
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
